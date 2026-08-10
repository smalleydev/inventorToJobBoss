"""
JobBOSS material matching logic.

Two families of part number, handled differently:

  JOB-SPECIFIC parts — three all-numeric segments, e.g. "28229-01-005"
  (job number - sub-assembly - item). Unique to one job; they will NEVER
  exist as their own JobBOSS material record. Looking them up by part
  number (exact match or prefix search) is pointless and can produce
  misleading "ambiguous" candidates from unrelated job-specific parts
  that happen to share a job-number prefix. These resolve through the
  Material field only.

  STANDARD parts — letters in a suffix (e.g. "045-509-SS") or two-segment
  vendor-style numbers (e.g. "20-0259"). These go through the full
  part-number -> description -> material chain.

Match priority, per part:
  1. Exact match: Inventor PartNumber == Material.Material
     (skipped for job-specific numbers)
  2. Exact match: Inventor Material field (vendor number, "VENDOR "
     prefix stripped) == Material.Material
  3. Embedded JB# reference: some descriptions carry a deliberate
     human-placed cross-reference like "(JB# 028-381)". Fully trusted —
     resolves clean, no flag. If the referenced number doesn't exist,
     falls through rather than trusting a broken reference.
  4. Raw stock extraction: the Material field is a raw-stock description
     ending in the actual JobBOSS material number, e.g.
     "SS SH 10GA X 48 X 120 T304 2B 28-0003" -> "28-0003". Applies to
     sheet (SH), plate (PL), tube (TU), and angle (AN) shape codes.
     UHMW is excluded from auto-resolution regardless of shape code and
     always routed to a human (needs_review_uhmw).
  5. Progressive prefix search: truncate the part number at each
     trailing hyphen segment (never below the first two segments) and
     search for anything starting with that base. Skipped for
     job-specific numbers.

     For STANDARD parts, step 5 also acts as a GATE on step 4: even when
     raw-stock extraction found a match, a human must confirm the part
     doesn't exist as its own JobBOSS material under a slightly
     different suffix before it's allowed to auto-resolve. Sheet/plate
     skip the gate — they're dropped from the export regardless, so
     precision on their material match doesn't matter, and gating them
     only risks false "ambiguous" flags.

If nothing matches, the part is unmatched (not_found) and needs full
manual resolution via the dialog.
"""

import re
from dataclasses import dataclass, field

from db import get_connection

# --- Pattern constants ------------------------------------------------------

# Job-specific part number: exactly three all-numeric segments, no letters.
JOB_SPECIFIC_PART_NUMBER = re.compile(r"^\d+-\d+-\d+$")

# Embedded "JB# <number>" cross-reference in a description, e.g.
# "(JB# 028-381)". Case-insensitive; tolerant of surrounding text.
JB_REFERENCE = re.compile(r"JB#\s*([A-Za-z0-9\-]+)", re.IGNORECASE)

# Trailing "<2-3 digits>-<3-5 digits>" token anchored to the end of the
# Material description, e.g. "28-0003" or "18-0025".
TRAILING_MATERIAL_NUMBER = re.compile(r"(\d{2,3}-\d{3,5})\s*$")

# Shape codes whose Material descriptions carry a trailing JobBOSS
# material number (step 4). Sheet/plate get special no-gate treatment;
# see FLAT_STOCK_SHAPE_CODES usage in lookup_material.
SHEET_PLATE_SHAPE_CODES = ("SH", "PL")
LINEAR_STOCK_SHAPE_CODES = ("TU", "AN", "BR")

# Minimum number of hyphen segments the prefix search will keep. Going
# below two (e.g. searching just "028") returns far too many unrelated
# candidates to be useful.
MIN_PREFIX_SEGMENTS = 2

# Cap on prefix-search candidate lists — a search returning hundreds of
# rows isn't a useful pick list for the dialog anyway.
MAX_PREFIX_CANDIDATES = 50


# --- Result types -----------------------------------------------------------

@dataclass
class MaterialCandidate:
    """One JobBOSS material offered as a possible match for a part."""
    material_number: str
    description: str
    is_raw_stock: bool = False


@dataclass
class LookupResult:
    """
    Outcome of one lookup_material() call.

    status values:
      exact_part        — PartNumber matched Material.Material directly
      exact_vendor      — vendor number (Material field) matched directly
      jb_reference      — embedded JB# cross-reference matched
      raw_stock_match   — trailing raw-stock number matched
      ambiguous         — prefix search found candidates; human must pick
      needs_review_uhmw — UHMW stock; human must confirm even if matched
      not_found         — nothing matched anywhere
    """
    status: str
    matched_material: str | None = None
    matched_description: str | None = None
    candidates: list[MaterialCandidate] = field(default_factory=list)
    searched_prefix: str | None = None


# --- Small helpers ----------------------------------------------------------

def is_job_specific_part_number(part_number: str) -> bool:
    """True for a three-segment all-numeric part number like '28229-01-005'."""
    return bool(part_number) and bool(JOB_SPECIFIC_PART_NUMBER.match(part_number))


def strip_vendor_prefix(material_field: str) -> str:
    """'VENDOR 11-0087' -> '11-0087'. Returns input unchanged if no prefix."""
    if material_field and material_field.upper().startswith("VENDOR "):
        return material_field[len("VENDOR "):].strip()
    return material_field


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcards so a literal %, _, or [ in a part number
    isn't treated as a pattern character. Pairs with ESCAPE '[' in the
    query."""
    return value.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")


def _has_shape_code(material_field: str, code: str) -> bool:
    """Whole-word match for a shape code like 'SH' or 'PL' — avoids
    matching inside unrelated tokens (e.g. 'SH' inside 'BUSHING')."""
    return re.search(rf"\b{code}\b", material_field) is not None


def _fetch_material(cursor, material_number: str):
    """Exact-match fetch of one Material row (or None). Centralizes the
    query all five lookup steps share."""
    cursor.execute(
        "SELECT Material, Description FROM Material WHERE Material = ?",
        material_number,
    )
    return cursor.fetchone()


def classify_secondary_category(material_field: str) -> str:
    """
    Categories NOT set by the Inventor extractor (which only tags TUBE,
    ROUND BAR, and BAR):

      SHEET / PLATE — no linear length concept; excluded from the
        JobBOSS export entirely (sheet metal team handles them).
      ANGLE — has a real length, but Inventor never pulled it for this
        category; flagged via the "Needs Length" traveler state instead,
        resolved by the user typing a value into the table.

    Returns "" for anything else.
    """
    if not material_field:
        return ""
    if _has_shape_code(material_field, "SH"):
        return "SHEET"
    if _has_shape_code(material_field, "PL"):
        return "PLATE"
    if _has_shape_code(material_field, "AN"):
        return "ANGLE"
    return ""


# --- Core lookup ------------------------------------------------------------

def lookup_material(cursor, part_number: str, material_field: str,
                    description: str = "") -> LookupResult:
    """
    Attempt to resolve one BOM row to a JobBOSS Material number.

    `cursor` is a live pyodbc cursor — the caller owns the connection.
    `description` is optional; only used for the embedded JB# check.

    See the module docstring for the full priority chain and the
    standard-part gate between steps 4 and 5.
    """
    job_specific = is_job_specific_part_number(part_number)

    # --- 1. Exact match on the part number itself -----------------------
    # Skipped for job-specific numbers — guaranteed not to exist as their
    # own JobBOSS material.
    if part_number and not job_specific:
        row = _fetch_material(cursor, part_number)
        if row:
            return LookupResult(
                status="exact_part",
                matched_material=row.Material,
                matched_description=row.Description,
            )

    # --- 2. Exact match on the vendor number (Material field) -----------
    vendor_number = strip_vendor_prefix(material_field)
    if vendor_number:
        row = _fetch_material(cursor, vendor_number)
        if row:
            return LookupResult(
                status="exact_vendor",
                matched_material=row.Material,
                matched_description=row.Description,
            )

    # --- 3. Embedded JB# reference in the description --------------------
    if description:
        jb_match = JB_REFERENCE.search(description)
        if jb_match:
            row = _fetch_material(cursor, jb_match.group(1))
            if row:
                return LookupResult(
                    status="jb_reference",
                    matched_material=row.Material,
                    matched_description=row.Description,
                )
            # JB# present but broken — fall through rather than trust it.

    # --- 4. Raw stock extraction ------------------------------------------
    # Held rather than returned immediately: for standard parts, step 5
    # must get a chance to surface a near-miss part-number match first.
    raw_stock_result: LookupResult | None = None
    is_sheet_or_plate = False

    if material_field:
        is_uhmw = "UHMW" in material_field.upper()
        is_sheet_or_plate = any(
            _has_shape_code(material_field, code) for code in SHEET_PLATE_SHAPE_CODES
        )
        is_raw_stock = is_sheet_or_plate or any(
            _has_shape_code(material_field, code) for code in LINEAR_STOCK_SHAPE_CODES
        )

        number_match = TRAILING_MATERIAL_NUMBER.search(material_field)

        if number_match:
            trailing_number = number_match.group(1)

            if is_uhmw:
                # UHMW never auto-resolves — return immediately so a human
                # confirms, with the candidate attached if it exists.
                row = _fetch_material(cursor, trailing_number)
                return LookupResult(
                    status="needs_review_uhmw",
                    matched_material=row.Material if row else None,
                    matched_description=row.Description if row else None,
                )

            if is_raw_stock:
                row = _fetch_material(cursor, trailing_number)
                if row:
                    raw_stock_result = LookupResult(
                        status="raw_stock_match",
                        matched_material=row.Material,
                        matched_description=row.Description,
                    )

    # Sheet/plate skip the step-5 gate entirely: they're dropped from the
    # JobBOSS export regardless of which material they resolve to, so
    # gating them only risks flipping a fine match into a false red flag.
    if raw_stock_result and is_sheet_or_plate:
        return raw_stock_result

    # --- 5. Progressive prefix search (and standard-part gate) -----------
    if part_number and not job_specific and "-" in part_number:
        segments = part_number.split("-")

        while len(segments) > MIN_PREFIX_SEGMENTS:
            segments = segments[:-1]
            base = "-".join(segments)

            cursor.execute(
                f"SELECT TOP {MAX_PREFIX_CANDIDATES} Material, Description "
                "FROM Material WHERE Material LIKE ? ESCAPE '['",
                _escape_like(base) + "%",
            )
            rows = cursor.fetchall()

            if rows:
                candidates = [MaterialCandidate(r.Material, r.Description) for r in rows]

                # The gated raw-stock match, if any, is still a valid
                # option — surface it alongside the prefix-search
                # candidates rather than discarding it, so a human can
                # pick it explicitly when none of the near-miss part
                # numbers are actually right.
                if raw_stock_result:
                    candidates.append(MaterialCandidate(
                        raw_stock_result.matched_material,
                        f"{raw_stock_result.matched_description} (raw stock — unconfirmed)",
                        is_raw_stock=True,
                    ))

                return LookupResult(
                    status="ambiguous",
                    candidates=candidates,
                    searched_prefix=base,
                )

    # Gate passed with no near-miss candidates — the held raw-stock match
    # (if any) is now safe to trust.
    if raw_stock_result:
        return raw_stock_result

    return LookupResult(status="not_found")


# --- Standalone smoke test ---------------------------------------------------

if __name__ == "__main__":
    # Exercises each branch against live TESTPROD data. Expected statuses
    # noted per case; run after any change to the matching logic.
    TEST_CASES = [
        # (part_number, material_field, description)
        ("045-509-SS", "SS PL .5 X 48 X 120 T304 2B 28-0475", ""),          # raw_stock (sheet/plate, no gate)
        ("NA", "VENDOR 11-0087", ""),                                        # exact_vendor
        ("20-0259", "VENDOR 20-0259", ""),                                   # exact_part
        ("28229-01-005", "SS SH 10GA X 60 X 120 T304 2B 28-0420", ""),       # job-specific raw_stock
        ("028-300-01-30", "UHMW SH 1 X 48 X 120 18-0025", ""),               # needs_review_uhmw
        ("028-0854-0045-SS01", "SS BR RD .75 T304 28-0153",
         "STAND-OFF, .75 X .375 X 1/4-20 (JB# 028-381)"),                    # jb_reference
        ("28229-01-105", "SS AN 3 X 3 X .25 T304 28-0063", ""),              # job-specific angle raw_stock
    ]

    conn = get_connection()
    test_cursor = conn.cursor()

    for pn, mat, desc in TEST_CASES:
        result = lookup_material(test_cursor, pn, mat, desc)
        print(f"\n{pn} / {mat}")
        print(f"  job_specific: {is_job_specific_part_number(pn)}")
        print(f"  status: {result.status}")
        if result.matched_material:
            print(f"  matched: {result.matched_material} — {result.matched_description}")
        if result.candidates:
            print(f"  searched prefix: {result.searched_prefix}")
            for c in result.candidates[:10]:
                print(f"  candidate: {c.material_number} — {c.description}")

    conn.close()
