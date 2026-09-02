"""
Traveler State — the single source of truth for row states in JL Check.

A row's Traveler State summarizes where it stands on the path to the
final JobBOSS traveler:

  Needs Attention  (red)    — no confirmed JobBOSS material yet; a human
                              must resolve it via the dialog.
  Needs Length     (orange) — the resolved JobBOSS material demands a cut
                              length that hasn't been entered yet. This is
                              true whenever the material is stocked by a
                              linear unit (Stocked_UofM is feet or inches),
                              and — separately — whenever a linear-stock
                              category matched via raw-stock extraction.
                              Resolved by typing a length into the table.
  Ignored          (yellow) — EXCLUDED from the final JobBOSS export.
                              Either sheet/plate with no exact JobBOSS
                              match (handled by the sheet metal team
                              through a separate process) or explicitly
                              ignored by the engineer.
  Attended         (clear)  — fully resolved, but a human had to step in
                              at some point. Same treatment as Clean in
                              the export; the label preserves provenance.
  Clean            (clear)  — fully resolved automatically, no human
                              involvement needed.
  Custom           (blue)   — added directly by the engineer via "Add
                              Custom Line," bypassing JobBOSS material
                              matching entirely. Not raw stock, not in
                              the material library — a one-off line new
                              to this quote only. Exported as-is; the
                              push service is responsible for writing it
                              as a JobBOSS Misc line rather than a
                              material-linked one.

Every state above is derived — there is no way for an engineer to force
a row directly to a given TravelerState. An earlier version had a
right-click "Set State" override; it was removed after it let a row
reach Finalize as Clean/Attended with no JobBossMaterial at all (see
project notes on the 28278-01A push failure). The fix now in place is
architectural rather than a guard: the only way to change a row's state
is to change the facts compute_traveler_state() looks at (MatchStatus,
JobBossMaterial, Category, CutLengthIn) via the resolve dialog or an
inline edit.

Rule order in compute_traveler_state:
  1. Explicit engineer ignore always wins.
  2. Custom line -> Custom, unconditionally. Set once at creation and
     never recomputed off of it — a custom line has no MatchStatus in
     the matching sense, no Category, nothing else in this function
     applies to it.
  3. Missing part number ("NA") -> Needs Attention, UNLESS the Material
     field starts with "VENDOR " (e.g. "VENDOR 11-0341"), which marks
     expected vendor-numbered hardware that legitimately has no Inventor
     part number. That case falls through to normal rules 5-8 instead.
  4. UHMW and Lexan (needs_review_uhmw / needs_review_lexan, see
     SPECIAL_REVIEW_STATUSES) -> Needs Attention, unconditionally, even
     if shape code makes Category read as SHEET/PLATE — both always
     need a human regardless of shape code (see jobboss_lookup.py).
     Must come before rule 5, or it gets silently swallowed into
     Ignored.
  5. SHEET/PLATE -> Ignored (dropped from export, handled by the sheet
     metal team) — UNLESS the part resolved via an EXACT JobBOSS match
     (exact_part / exact_vendor / jb_reference — see EXACT_MATCH_STATUSES).
     An exact match means this specific sheet/plate item IS itself a
     real, standalone JobBOSS material, not raw stock the sheet metal
     team needs to nest by hand — it should flow into the export using
     that material number rather than being dropped. raw_stock_match
     does NOT qualify here: that's an inferred match off a shape-code
     description, not a confirmed part-number match, so sheet/plate raw
     stock still goes to the sheet metal team's separate process. An
     unresolved lookup (not_found/ambiguous/etc.) also still lands in
     Ignored — checking category before the general match-status check
     below is what lets a sheet/plate part with no clean material match
     land in Ignored instead of getting stuck in Needs Attention.
  6. Unresolved statuses -> Needs Attention. Length is never checked
     before material identity is settled.
  7. Needs Length, checked two independent ways, either one enough:
       a. The resolved material's own Stocked_UofM is feet or inches
          (see LENGTH_REQUIRED_UOFM) — that material is sold by a
          linear unit, so a length is required no matter how the match
          was made (exact part/vendor/JB# match included) or what
          Category says.
       b. A linear-stock CATEGORY (LENGTH_REQUIRED_CATEGORIES) matched
          via raw-stock extraction (RAW_STOCK_STATUSES) — the older,
          narrower check, kept as a fallback for materials whose
          Stocked_UofM isn't set to FT/IN in JobBOSS but are still
          raw stock by shape code.
     Either way, only fires once a real JobBossMaterial exists — an
     unresolved row already stopped at rule 6.
  8. Otherwise Attended (human resolved) or Clean (auto-matched), and
     never either one without a real JobBossMaterial (belt-and-
     suspenders: if this ever falls through with no material, that's an
     upstream bug and Needs Attention is the safe failure mode).
"""

# MatchStatus values that count as an EXACT JobBOSS match — the part
# number (or vendor number, or an embedded JB# reference) matched a
# real Material row directly, as opposed to being inferred from a
# shape-code description (raw_stock_match) or left for a human
# (ambiguous / needs_review_uhmw / needs_review_lexan / not_found).
# Used to let an exact-matched sheet/plate part escape the normal
# SHEET/PLATE auto-ignore in rule 4.
EXACT_MATCH_STATUSES = frozenset({"exact_part", "exact_vendor", "jb_reference"})

# Statuses jobboss_lookup.py routes to unconditionally when the
# material's own keyword marks it as a plastic that always needs a
# human, regardless of shape code (UHMW: "UHMW"; Lexan: "LEX") — see
# rule 4. Kept as one set so a future addition of the same kind (a
# third such keyword) only needs to change jobboss_lookup.py plus this
# one line, not every place that currently special-cases "uhmw".
SPECIAL_REVIEW_STATUSES = frozenset({"needs_review_uhmw", "needs_review_lexan"})

# MatchStatus values that mean "no confirmed JobBOSS material yet".
UNRESOLVED_STATUSES = frozenset({
    "ambiguous",
    "not_found",
    "new_material_needed",
}) | SPECIAL_REVIEW_STATUSES

# Categories excluded from the JobBOSS export entirely.
EXCLUDED_CATEGORIES = frozenset({"SHEET", "PLATE"})

# Categories that carry a real linear cut length.
LENGTH_REQUIRED_CATEGORIES = frozenset({"TUBE", "ANGLE", "BAR", "ROUND BAR"})

# Statuses where the JobBOSS material IS raw stock — meaning the length
# had to come from the CAD model, not from the material record itself.
# Kept as the narrower, category-driven fallback for rule 7b; rule 7a's
# Stocked_UofM check is the primary signal now and applies regardless of
# MatchStatus or Category.
RAW_STOCK_STATUSES = frozenset({"raw_stock_match", "resolved_manual_raw_stock"})

# Material.Stocked_UofM values that mean "sold by a linear unit" — any
# resolved material stocked this way demands a cut length (rule 7a),
# full stop, regardless of Category or how the match was made. Values
# are matched case-insensitively after stripping whitespace (JobBOSS
# stores these lowercase — "ft", "in" — but "FT"/"IN" show up too).
# The other UofM values seen in this JobBOSS instance, "ea" and
# "pack"/"PACK", are deliberately absent — neither implies a linear
# length, so they fall through to the ordinary Clean/Attended rule 8.
LENGTH_REQUIRED_UOFM = frozenset({"FT", "IN"})

# Display/sort priority: lower number sorts first. Alphabetical order
# would bury "Needs Attention" below "Attended" — this keeps the rows
# that need human eyes at the top. Imported by main.py; keep the state
# names here in sync with compute_traveler_state's return values.
SORT_ORDER = {
    "Needs Attention": 0,
    "Needs Length": 1,
    "Ignored": 2,
    "Attended": 3,
    "Clean": 4,
    "Custom": 5,
}


def compute_traveler_state(row: dict) -> str:
    """Compute the Traveler State for a row dict carrying MatchStatus,
    Category, and CutLengthIn. Safe to call repeatedly — it derives the
    state fresh from current values each time. There is no manual
    override input anymore: TravelerState is always a pure function of
    the row's other fields."""
    status = row.get("MatchStatus")
    category = row.get("Category", "")
    has_material = bool(row.get("JobBossMaterial"))

    # 1. Explicit engineer override — always wins.
    if status == "manually_ignored":
        return "Ignored"

    # 2. Custom line item — added directly via "Add Custom Line," never
    # touched jobboss_lookup, and nothing else in this function applies
    # to it (no Category to check, no match to be unresolved).
    if status == "custom_line":
        return "Custom"

    # 3. Missing part number — Inventor had no real part number for this
    # item. Forces Needs Attention regardless of MatchStatus or Category,
    # UNLESS the Material field identifies it as vendor-numbered hardware
    # (starts with "VENDOR ", e.g. "VENDOR 11-0341") — that's an expected,
    # legitimate NA case (vendor parts have no Inventor part number by
    # design), not a data gap worth a human glance. In that case we fall
    # through to the normal rules below (5-8), so an exact vendor match
    # still resolves to Clean/Attended while an unresolved one still
    # correctly lands in Needs Attention via rule 6.
    # Checked after explicit engineer actions (ignore, custom line) —
    # those still win — but before everything else, so a genuinely
    # missing part number is never silently resolved away.
    if (row.get("PartNumber") or "").strip().upper() == "NA":
        material = (row.get("Material") or "").strip().upper()
        if not material.startswith("VENDOR "):
            return "Needs Attention"

    # 4. UHMW and Lexan always need a human, full stop — jobboss_lookup.py
    # routes them to needs_review_uhmw / needs_review_lexan regardless of
    # shape code, specifically because both can carry a SHEET/PLATE shape
    # code (e.g. "UHMW SH .5 X 48 X 120...", "LEX SH .25 X 48 X 96...")
    # without actually being sheet-metal-team material. Must be checked
    # BEFORE the SHEET/PLATE auto-ignore below, or a row like this gets
    # silently swallowed into Ignored instead of surfacing for review.
    if status in SPECIAL_REVIEW_STATUSES:
        return "Needs Attention"

    # 5. Sheet/plate: excluded from the export, handled by the sheet
    # metal team — UNLESS this part resolved via an EXACT JobBOSS match
    # (see EXACT_MATCH_STATUSES). An exact match means the part itself
    # is a real, standalone JobBOSS material, not raw stock to be
    # nested by hand, so it should flow into the export instead of
    # being dropped. Checked BEFORE the general unresolved-status check
    # below — a sheet/plate part with no clean material match (or only
    # a raw_stock_match, which doesn't qualify here) must still land in
    # Ignored regardless of MatchStatus, rather than getting stuck in
    # Needs Attention.
    if category in EXCLUDED_CATEGORIES and status not in EXACT_MATCH_STATUSES:
        return "Ignored"

    # 6. Material identity comes before everything else (for categories
    # that actually need a confirmed material).
    if status in UNRESOLVED_STATUSES:
        return "Needs Attention"

    # 7. Needs Length — two independent triggers, either one enough.
    # Both only apply once a real material is on the row; an unresolved
    # lookup already returned "Needs Attention" at rule 6 above.
    if has_material and row.get("CutLengthIn", 0) in (0, 0.0):
        # 7a. The resolved material itself is stocked by a linear unit
        # (feet/inches) — demands a length no matter how it was matched
        # or what Category says. This supersedes the old assumption that
        # an exact part/vendor/JB# match never needs one.
        uofm = (row.get("JobBossUofM") or "").strip().upper()
        if uofm in LENGTH_REQUIRED_UOFM:
            return "Needs Length"

        # 7b. Fallback: a linear-stock CATEGORY matched via raw-stock
        # extraction, for materials whose Stocked_UofM isn't set to
        # FT/IN in JobBOSS but are still raw stock by shape code.
        if category in LENGTH_REQUIRED_CATEGORIES and status in RAW_STOCK_STATUSES:
            return "Needs Length"

    # 8. Done — distinguish "a human fixed this" from "matched on its
    # own". Never report Clean/Attended without a real JobBossMaterial:
    # if this ever triggers it means something upstream (jobboss_lookup,
    # the resolve dialog) produced an inconsistent row — Needs Attention
    # is the safe failure mode, not a silent export.
    if not has_material:
        return "Needs Attention"

    return "Attended" if status in ("resolved_manual", "resolved_manual_raw_stock") else "Clean"