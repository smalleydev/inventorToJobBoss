# Feature Workflow — InventorToJobBoss

How to make a change, get it merged, and get it actually running — for
any of the three projects in this repo (`jl_check`, `bompush_service`,
`BomPushAddIn`).

## The three locations, and why they're separate

| Location | Role | Rule |
|---|---|---|
| `C:\Users\lstrain\source\InventorToJobBoss` | Where you actually develop | Edit here. Never edited on F:\. |
| GitHub (`smalleydev/inventorToJobBoss`) | Source of truth | Every real change passes through here. |
| `F:\BOMIntegration\Git\inventorToJobBoss` | Stable mirror | Pull-only. Never developed on, never built from directly. |
| `F:\BOMIntegration\Service\Watchdog`, `Releases\BOMFormatter`, `Releases\BomPushAddIn` | Deployed/running copies | Populated only from a local build of `F:\Git`, never from `C:\` dev directly. |

The chain is always: **`C:\` dev → GitHub → `F:\Git` → local build →
deployed location.** Skipping a hop (e.g. copying straight from `C:\`
dev into a deployed folder) breaks the guarantee that whatever's
running traces back to a real, reviewable commit. This has actually
bitten us once already — worth not repeating.

---

## Step 1 — Branch

```
cd C:\Users\lstrain\source\InventorToJobBoss
git checkout main
git pull
git checkout -b feature/<short-description>
```

**Why:** `git pull` before branching so you're starting from the real
current state of `main`, not a stale local copy. A fresh branch per
piece of work (not reusing an old, already-merged one) keeps `git log`
meaningful later — you can tell what shipped when just from branch
names/PRs, not just from digging through commit messages.

---

## Step 2 — Make the change, test it locally

Edit files under `C:\...\InventorToJobBoss\<project>\` as normal. Test
using whatever that project's normal local dev loop is (run
`main.py`/`watcher_service.py` directly against your dev venv, or open
the `.vbproj` in Visual Studio for the add-in). Nothing shared gets
touched at this stage.

**Check before moving on:** does it actually work locally? Don't skip
straight to committing on the assumption a change is correct — the
whole point of this stage is catching problems before they're on a
branch anyone else might look at.

---

## Step 3 — Commit

```
git add <specific files>
git status
```

**Why `git add` with explicit filenames, not `git add .`:** confirms
exactly what's being staged before it happens — cheap insurance
against accidentally sweeping in a stray test-output file, a
misspelled filename, or something `.gitignore` doesn't happen to catch.

Check the `git status` output actually shows only the files you meant
to change. Then:

```
git commit -m "<clear, specific message>"
git status
```

That last `git status` should read `nothing to commit, working tree
clean` — confirms the commit actually captured everything, nothing got
left behind unstaged (e.g. from a multi-line command that silently
split across two PowerShell commands and only staged half of what you
intended — this has happened before).

---

## Step 4 — Merge to `main`, push

```
git checkout main
git pull
git merge feature/<short-description>
git push origin main
```

**Why `git pull` again here, right before merging:** `main` may have
moved since you branched (someone else's push, or your own earlier
work). Merging into a stale local `main` risks a rejected push later
for the same reason a stale branch does — cheap to re-check.

**Check:** the merge should say `Fast-forward` (clean, no conflicts to
resolve) for anything following this workflow one branch at a time.
The `git push` should end with something like `<old>..<new>  main ->
main` — if it's rejected instead, `git pull` again before retrying;
don't force-push.

---

## Step 5 — Pull on `F:\Git`

```
cd F:\BOMIntegration\Git\inventorToJobBoss
git pull
```

**Why this is a separate, required step, not optional:** `F:\Git` is
its own independent clone — pushing to GitHub does not update it
automatically. Every deployment in Step 6 below starts from *this*
folder, so if you skip this pull, you'll build/deploy the previous
version without any error telling you so.

**Check:** should say `Fast-forward` and list the files that changed —
confirm the files you expect to see actually appear in that list.

---

## Step 6 — Build and deploy (only if the change affects a running/shared component)

Not every change needs this step — a fix that only affects local dev
tooling (e.g. `db_test.py`) doesn't need rebuilding or redeploying
anywhere. Skip to Step 7 if nothing shared changed.

### 6a — `bompush_service` (no build step — it runs as raw Python)

```
robocopy F:\BOMIntegration\Git\inventorToJobBoss\bompush_service C:\BomPushService /MIR /XD __pycache__ venv .venv /XF *.pyc
```

**Why local disk (`C:\BomPushService`), not run directly off `F:\Git`
or `Service\Watchdog`:** a live Python process importing modules over a
network share is fragile — slower I/O, and a share hiccup can crash or
hang a process that's supposed to run unattended. `/MIR` (not `/E`)
because this should be an exact mirror — old files that no longer
exist in source shouldn't linger in the deployed copy.

If `requirements.txt` changed:
```
cd C:\BomPushService
venv\Scripts\activate
pip install -r requirements.txt
```

**Restart the service** (stop the running `python watcher_service.py`
with Ctrl+C, start it again) — it doesn't hot-reload.

**Check:** startup log line should show it watching the correct UNC
`Incoming` path. Do a real end-to-end test — Finalize a BOM from JL
Check, confirm the file gets claimed and moves to `Completed` (or
`Error` with a real reason), and confirm a matching line appears in
`\\SYS\sys\BOMIntegration\Logs\bompush_service.log`.

### 6b — `jl_check` (PyInstaller build)

```
robocopy F:\BOMIntegration\Git\inventorToJobBoss\jl_check C:\JLCheckBuild /MIR /XD __pycache__ venv .venv
cd C:\JLCheckBuild
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pip install pyinstaller
pyinstaller --onefile --windowed --name JLCheck main.py
```

**Why a fresh venv every time** rather than reusing one: guarantees
the build reflects exactly `requirements.txt`, not whatever happens to
already be installed from a previous build.

**Check before deploying — don't skip this:**
```
.\dist\JLCheck.exe
```
Confirm it actually opens (catches a missing PySide6 platform plugin,
which build success alone won't catch) AND confirm it can actually
reach TESTPROD (catches a `pyodbc`/ODBC driver bundling issue,
separately from the window just opening).

```
robocopy C:\JLCheckBuild\dist F:\BOMIntegration\Releases\BOMFormatter JLCheck.exe
```

**Check:** file size/timestamp on the deployed copy matches what was
just built (`Get-Item ... | Select-Object Length, LastWriteTime`).

### 6c — `BomPushAddIn` (VB.NET build)

```
robocopy F:\BOMIntegration\Git\inventorToJobBoss\BomPushAddIn C:\BomPushAddInBuild /MIR /XD bin obj
cd C:\BomPushAddInBuild
dotnet build -c Release
```

**Why `-c Release`, not the default Debug build:** slower at runtime
and pulls in debug behavior/symbols you don't want loaded into every
engineer's live Inventor session.

**Close Inventor before redeploying the DLL** — it locks the file
while loaded. Robocopy will retry automatically if it's still open,
but you don't control exactly when the new version takes effect if you
leave it running (the next Inventor restart, on any machine, picks up
whatever's on the share at that moment).

```
robocopy C:\BomPushAddInBuild\bin\Release F:\BOMIntegration\Releases\BomPushAddIn BomPushAddIn.dll
```

**Only re-copy the `.addin` manifest too if `BomPushAddIn.addin`
itself changed** (its `<Assembly>` path, `ClassId`, etc.) — not on
every DLL rebuild:
```
robocopy C:\BomPushAddInBuild F:\BOMIntegration\Releases\BomPushAddIn BomPushAddIn.addin
```

If the manifest changed, every engineer needs to re-run
`install_bompush_addin.bat` (see below) — a DLL-only change needs
nothing from them at all; their next Inventor launch just picks it up.

**Check:** restart Inventor, confirm the add-in loads and the specific
behavior you changed actually works.

---

## Step 7 — Rolling out to other engineers (add-in only, one-time or when the manifest changes)

Each engineer runs `F:\BOMIntegration\Releases\BomPushAddIn\install_bompush_addin.bat`
once, with Inventor closed. It:
- Confirms Inventor isn't running (a locked file fails silently otherwise)
- Removes any old bundle-style install (same `ClassId`/`ClientId` — leaving
  both registered risks a conflict)
- Copies the current manifest into their local `Addins` folder

After that, they need nothing else, ever again, for ordinary DLL/exe
updates — both `BomPushAddIn.dll` and `JLCheck.exe` load live off the
share on every launch.

---

## Known pitfalls (already hit once — don't re-debug these from scratch)

- **Git's bundled SSH client ≠ Windows OpenSSH.** On this machine, Git
  for Windows' own `ssh.exe` resolves `HOME` to `/u/` (from
  `HOMEDRIVE=U:`, a domain-provisioned home drive) instead of your real
  profile, so it can't find your real key even though `ssh -T
  git@github.com` works fine manually. Fixed permanently via:
  `[System.Environment]::SetEnvironmentVariable("GIT_SSH_COMMAND", "C:/Windows/System32/OpenSSH/ssh.exe", "User")`
- **`git config --global` writes to `U:\.gitconfig`**, not
  `C:\Users\lstrain\.gitconfig` — same `HOMEDRIVE` cause. Not wrong,
  just worth knowing if global config ever seems to "not take."
- **UNC paths need `safe.directory`.** First `git` command against
  `F:\Git` will fail with "detected dubious ownership" — run the
  `git config --global --add safe.directory` line it suggests, once.
- **A locked DLL doesn't fail loudly** with a plain `copy` — robocopy
  at least retries and reports it. Close Inventor before redeploying
  `BomPushAddIn.dll`.
- **PowerShell doesn't expand `%VAR%`** (that's cmd syntax) — use
  `$env:VAR` / `echo $env:VAR` instead.
- **A multi-line `git add` can silently split across commands** if
  pasted oddly — always confirm with `git status` before committing,
  not after.