# Code Review — InventorToJobBoss

Pass over `BomPushAddIn`, `bompush_service`, and `jl_check` as of commit
`fea4093`. Organized by severity, not by file — the things worth
looking at first are at the top.

---

## Database-safety concerns

### 1. No safety net around `get_connection()` at JL Check startup — **worth fixing**

`main.py`'s `MainWindow.__init__` calls `self.conn = get_connection()`
with nothing catching a failure. If TESTPROD is unreachable (VPN down,
SQL Server restarting, wrong environment), `db.py` raises — and since
this is now a `--onefile --windowed` PyInstaller build, that exception
has **no console to print to**. From the engineer's seat, double-clicking
`JLCheck.exe` would just silently do nothing. This was invisible during
development (a stack trace prints to the terminal you launched it
from) but is now a real support-call generator once every engineer is
running the packaged `.exe`.

**Suggested fix:** wrap the connection attempt in `main()`, before
`MainWindow()` is constructed, and show a `QMessageBox` with the actual
error before exiting — so a DB outage looks like a clear message, not
a phantom non-launch.

### 2. `jobboss_keys.py` is dead code — **confirm and likely remove**

`next_key()` — the `p_GetNextKey`-style atomic-increment helper — is
never imported anywhere in either `bompush_service` or `jl_check`.
`quote_writer.py` uses a different, since-superseded mechanism instead
(`_insert_and_get_generated_key`, relying on `Quote_Qty`/`Quote_Req`/
`Quote_Req_Qty`'s `AFTER INSERT` triggers + `SCOPE_IDENTITY()` — see
`ARCHITECTURE.md` for why that turned out to be the right approach).
Worth either deleting this file or adding a one-line header noting
it's kept as a reference for how JobBOSS's own key generation works,
not because anything calls it — as-is, a future reader will
reasonably assume it's load-bearing somewhere and waste time tracing
where.

### 3. `db.py` is byte-for-byte duplicated between `jl_check` and `bompush_service` — **DRY concern, real risk at PRODUCTION cutover**

Confirmed identical (`diff` returns nothing). Not a bug today, but the
`EXPECTED_DATABASE = "TESTPROD"` safety check — the actual mechanism
that stops a typo'd connection string from silently writing to
PRODUCTION — lives in **two files that have to be updated in lockstep,
by hand, with no shared source**. The eventual PRODUCTION cutover is
exactly the moment this matters: it's easy to update one copy, test
`jl_check` successfully, and forget `bompush_service` still points at
TESTPROD (or the reverse — worse, forget to update the guard while the
connection string moves). Worth either extracting to a genuinely
shared module both projects import (would need a small local package,
not just a copy), or — cheaper — adding this to `FEATURE_WORKFLOW.md`
as an explicit two-file checklist item for that cutover so it's not
tribal knowledge.

### 4. `staging.py`'s duplicate-key detection checks a broad SQLSTATE class — **minor robustness note, not urgent**

`try_claim_quote` catches `pyodbc.IntegrityError` and checks
`exc.args[0] == "23000"`. `23000` is SQL Server's *general* integrity-
constraint-violation class — it covers primary-key violations, but
also foreign-key and check-constraint violations if `BOM_Staging_Header`
ever gains one. Today the table almost certainly only has the
`QuoteNumber` primary key, so this is correct in practice. If the
table's constraints ever grow, this would silently treat an unrelated
constraint failure as "someone else already claimed this quote" and
fall through to the reclaim-on-ERROR path instead of surfacing the
real problem. Not worth changing now; worth a comment noting the
assumption, so a future schema change trips over it in review instead
of in production.

### 5. Real DELETE path exists and is correctly gated — **no action, noted for awareness**

`quote_locked_dialog.py`'s `_delete_jobboss_quote` is the one place in
the whole codebase that issues real `DELETE` statements against
`Quote`/`Quote_Req`/`Quote_Req_Qty`/`Bill_Of_Quotes` — part of the
"Unlock and Rebuild" flow for a quote number stuck in `IMPORTED`. It's
behind an explicit, strongly-worded `QMessageBox.warning` confirmation
before it runs, and it never touches the `RFQ` master row (other
quotes may share it). This is good practice already in place — flagged
here only so it's documented as *the* place to look if a "why did a
quote disappear" question ever comes up.

---

## Good-practice notes (things already done right, worth preserving)

- **Every SQL statement across all three projects uses parameterized
  queries (`?` placeholders)** — no string-interpolated user data
  anywhere. The one place a query is built with an f-string
  (`resolve_dialog.py`'s `_show_material_details`, interpolating
  `DETAIL_FIELDS` into a column list) interpolates a **module-level
  constant**, not user input, and is correctly commented as such.
- **`db.get_connection()`'s environment guard** (verify `DB_NAME()`
  matches `EXPECTED_DATABASE` before handing back a live connection) is
  exactly the right shape for this kind of risk — checked once, at the
  one choke point every script goes through, rather than trusted
  per-caller.
- **Transactional boundaries are correct.** `watcher_service.py`
  disables autocommit, and `write_quote()` performs the whole
  RFQ→Quote→Quote_Qty→Quote_Req→Quote_Req_Qty sequence as one
  transaction supplied by the caller — a failure partway through never
  leaves a partial quote sitting in JobBOSS. `staging.py`'s claim/
  mark-imported/mark-error calls deliberately commit on their own,
  separate from the write transaction, which is the right call: the
  claim needs to be visible to other instances immediately, and the
  final status needs to be recorded regardless of whether the write
  transaction committed or rolled back.
- **The primary-key claim lock in `try_claim_quote`** is a genuinely
  correct pattern for cross-process mutual exclusion without needing
  `DELETE` rights or a stored procedure — verified under real
  concurrent load (`race_test.py`) and under a hard-killed process
  (`mid_write_kill_test.py`/`_verify.py`), not just asserted.

---

## Comments / documentation quality

Overall strong — most files carry module-level docstrings explaining
*why*, not just what, and several encode specific facts confirmed via
SQL Profiler trace rather than assumption (`quote_writer.py`'s header
is the best example of this — see `ARCHITECTURE.md` for the full list
pulled out into one place). A few gaps:

- `BomExtractor.vb`'s `MergeGroup` has a live `TODO` (cut-length
  conflicts aren't checked, only material conflicts — duplicate part
  numbers with differing lengths currently silently collapse to the
  first occurrence's length). Worth resolving or at least confirming
  it's still an acceptable gap before cut lengths from a merged row
  feed stock nesting for a part that's genuinely inconsistent across
  occurrences.
- `BomLineItem.vb` has five properties marked `RESERVED — not yet
  populated` (`MakeBuy`, `StockNumber`, `CutLengthCm`,
  `FlatPatternArea`, `Mass`). Harmless as-is (they serialize as
  default values, e.g. `0`/`null`, and nothing downstream reads them),
  but worth a periodic check that nothing's silently come to depend on
  one of them being real data.

---

## Summary

Nothing found that risks corrupting JobBOSS data today — the
transactional boundaries, parameterization, and the claim-lock are all
sound, and have empirical tests behind the claims that matter most
(the race condition and the hard-kill scenario). The two things worth
actually doing something about are **#1** (silent-failure risk on the
packaged `.exe`, now that there's no console to catch it) and **#3**
(the duplicated `db.py` safety check, specifically because of what's
riding on it at the PRODUCTION cutover).