"""
Traveler State — the single source of truth for row states in JL Check.

A row's Traveler State summarizes where it stands on the path to the
final JobBOSS traveler:

  Needs Attention  (red)    — no confirmed JobBOSS material yet; a human
                              must resolve it via the dialog.
  Needs Length     (orange) — the JobBOSS material IS raw stock (matched
                              via raw-stock extraction), and no cut length
                              is present yet. Resolved by typing a length
                              into the table.
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

Rule order in compute_traveler_state:
  1. Explicit engineer ignore always wins.
  2. Custom line -> Custom, unconditionally. Set once at creation and
     never recomputed off of it — a custom line has no MatchStatus in
     the matching sense, no Category, nothing else in this function
     applies to it.
  3. Missing part number ("NA") -> Needs Attention, unconditionally,
     even if the vendor number/Material field resolved cleanly on its
     own. A part with no real Inventor part number is worth a human
     glance regardless of how well the rest of it matched.
  4. UHMW (needs_review_uhmw) -> Needs Attention, unconditionally, even
     if its shape code makes Category read as SHEET/PLATE — UHMW always
     needs a human regardless of shape code (see jobboss_lookup.py).
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
  7. Linear-stock categories matched via raw-stock extraction, with no
     length -> Needs Length. If the part number matched its own
     dedicated JobBOSS material directly (exact_part / exact_vendor /
     jb_reference), that's a specific stocked item, not raw stock — no
     length is needed regardless of category.
  8. Otherwise Attended (human resolved) or Clean (auto-matched).
"""

# MatchStatus values that count as an EXACT JobBOSS match — the part
# number (or vendor number, or an embedded JB# reference) matched a
# real Material row directly, as opposed to being inferred from a
# shape-code description (raw_stock_match) or left for a human
# (ambiguous / needs_review_uhmw / not_found). Used to let an
# exact-matched sheet/plate part escape the normal SHEET/PLATE
# auto-ignore in rule 4.
EXACT_MATCH_STATUSES = frozenset({"exact_part", "exact_vendor", "jb_reference"})

# MatchStatus values that mean "no confirmed JobBOSS material yet".
UNRESOLVED_STATUSES = frozenset({
    "ambiguous",
    "needs_review_uhmw",
    "not_found",
    "new_material_needed",
})

# Categories excluded from the JobBOSS export entirely.
EXCLUDED_CATEGORIES = frozenset({"SHEET", "PLATE"})

# Categories that carry a real linear cut length.
LENGTH_REQUIRED_CATEGORIES = frozenset({"TUBE", "ANGLE", "BAR", "ROUND BAR"})

# Statuses where the JobBOSS material IS raw stock — meaning the length
# had to come from the CAD model, not from the material record itself.
# A part number that matched its own dedicated JobBOSS material directly
# (exact_part / exact_vendor / jb_reference) is a specific, already-cut
# stocked item and needs no length input at all.
RAW_STOCK_STATUSES = frozenset({"raw_stock_match", "resolved_manual_raw_stock"})

# Valid TravelerState values a manual override may be set to. Kept here
# (not just SORT_ORDER's keys) so compute_traveler_state can validate a
# stray/garbage value in the row dict rather than trusting it blindly.
ALL_STATES = frozenset({"Needs Attention", "Needs Length", "Ignored", "Attended", "Clean", "Custom"})

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
    state fresh from current values each time."""
    status = row.get("MatchStatus")
    category = row.get("Category", "")

    # 0. Manual override — an engineer explicitly set the state via the
    # working table's right-click menu (e.g. flipping a wrongly-Ignored
    # SHEET row back to Needs Attention, or forcing a resolved row back
    # to Needs Attention for a second look). This wins over every other
    # rule, including the category/status-driven ones below, and stays
    # pinned even through later inline edits (setData always calls back
    # through this function). Cleared by picking "Auto" from the same
    # menu, which removes ManualTravelerState from the row entirely.
    override = row.get("ManualTravelerState")
    if override in ALL_STATES:
        return override

    # 1. Explicit engineer override — always wins.
    if status == "manually_ignored":
        return "Ignored"

    # 2. Custom line item — added directly via "Add Custom Line," never
    # touched jobboss_lookup, and nothing else in this function applies
    # to it (no Category to check, no match to be unresolved).
    if status == "custom_line":
        return "Custom"

    # 3. Missing part number — Inventor had no real part number for this
    # item (typically vendor hardware identified only by its Material/
    # vendor field; see jobboss_lookup.py's "NA" test case). Even if
    # that vendor number happens to resolve cleanly (exact_vendor, or
    # now escapes SHEET/PLATE auto-ignore via an exact match under rule
    # 5), a missing part number is itself worth a human glance, so it
    # forces Needs Attention regardless of MatchStatus or Category.
    # Checked after explicit engineer actions (manual override, ignore,
    # custom line) — those still win — but before everything else, so
    # it's never silently resolved away.
    if (row.get("PartNumber") or "").strip().upper() == "NA":
        return "Needs Attention"

    # 4. UHMW always needs a human, full stop — jobboss_lookup.py routes
    # it to needs_review_uhmw regardless of shape code, specifically
    # because UHMW can carry a SHEET/PLATE shape code (e.g. "UHMW SH .5
    # X 48 X 120...") without actually being sheet-metal-team material.
    # Must be checked BEFORE the SHEET/PLATE auto-ignore below, or a
    # UHMW row with shape code SH/PL gets silently swallowed into
    # Ignored instead of surfacing for review.
    if status == "needs_review_uhmw":
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

    # 7. Raw-stock linear items without a length aren't ready yet.
    if category in LENGTH_REQUIRED_CATEGORIES and status in RAW_STOCK_STATUSES:
        if row.get("CutLengthIn", 0) in (0, 0.0):
            return "Needs Length"

    # 8. Done — distinguish "a human fixed this" from "matched on its own".
    return "Attended" if status in ("resolved_manual", "resolved_manual_raw_stock") else "Clean"