"""Characterization tests for jobboss_lookup.lookup_material against a fake Material table."""

import pytest

from jobboss_lookup import (
    _escape_like,
    classify_secondary_category,
    is_job_specific_part_number,
    lookup_material,
    strip_vendor_prefix,
)
from tests.fakes import FakeCursor, material_table

MATERIALS = {
    "11-0087": {"Description": "HHCS 1/4-20 X 1", "Stocked_UofM": "ea"},
    "028-381": {"Description": "GUSSET", "Stocked_UofM": "ea"},
    "28-0003": {"Description": "A513 TUBE 1.5 RD", "Stocked_UofM": "ft"},
    "18-0025": {"Description": "HRS SHEET 10GA", "Stocked_UofM": "ea"},
    "18-0100": {"Description": "UHMW SHEET .5", "Stocked_UofM": "ea"},
    "18-0300": {"Description": "LEXAN SHEET .25", "Stocked_UofM": "ea"},
    "28-0200": {"Description": "UHMW ROD .75", "Stocked_UofM": "ft"},
    "123-456-001": {"Description": "NEAR MISS A"},
    "123-456-002": {"Description": "NEAR MISS B"},
}

JOB_PART = "28229-01-005"  # job-specific: never looked up as its own material


@pytest.fixture
def cursor():
    return FakeCursor(material_table(MATERIALS))


def looked_up(cursor, value):
    return any(c.params and c.params[0] == value for c in cursor.calls)


# --- Helpers ----------------------------------------------------------------

@pytest.mark.parametrize("pn,expected", [
    ("28229-01-005", True),
    ("007-0302-0003", False),  # three numeric segments, wrong shape
    ("28229-1-005", False),
    ("", False),
])
def test_is_job_specific_part_number(pn, expected):
    assert is_job_specific_part_number(pn) is expected


def test_strip_vendor_prefix():
    assert strip_vendor_prefix("VENDOR 11-0087") == "11-0087"
    assert strip_vendor_prefix("vendor  11-0087 ") == "11-0087"
    assert strip_vendor_prefix("A36 STEEL") == "A36 STEEL"
    assert strip_vendor_prefix("") == ""


def test_escape_like():
    assert _escape_like("A_B%C[D") == "A[_]B[%]C[[]D"


@pytest.mark.parametrize("material,expected", [
    ("HRS SH 10GA 18-0025", "SHEET"),
    ("A36 PL .25 X 4", "PLATE"),
    ("A36 AN 2 X 2 X .25", "ANGLE"),
    ("BUSHING BRONZE", ""),  # no whole-word SH
    ("", ""),
])
def test_classify_secondary_category(material, expected):
    assert classify_secondary_category(material) == expected


# --- Lookup chain -----------------------------------------------------------

def test_step1_exact_part(cursor):
    r = lookup_material(cursor, "11-0087", "")
    assert (r.status, r.matched_material, r.matched_uofm) == ("exact_part", "11-0087", "ea")


def test_step2_exact_vendor_and_job_part_skipped(cursor):
    r = lookup_material(cursor, JOB_PART, "VENDOR 11-0087")
    assert r.status == "exact_vendor"
    assert r.matched_material == "11-0087"
    assert not looked_up(cursor, JOB_PART)


def test_step2_material_field_without_vendor_prefix_is_tried_as_is(cursor):
    assert lookup_material(cursor, JOB_PART, "11-0087").status == "exact_vendor"


@pytest.mark.parametrize("desc", ["GUSSET (JB# 028-381)", "gusset jb #028-381", "JB#028-381"])
def test_step3_jb_reference(cursor, desc):
    r = lookup_material(cursor, JOB_PART, "", description=desc)
    assert (r.status, r.matched_material) == ("jb_reference", "028-381")


def test_step3_broken_jb_reference_falls_through(cursor):
    assert lookup_material(cursor, JOB_PART, "", description="JB# 999-999").status == "not_found"


def test_step4_raw_stock_for_job_part(cursor):
    r = lookup_material(cursor, JOB_PART, "A513 TU RD 1.5 X .120 28-0003")
    assert (r.status, r.matched_material, r.matched_uofm) == ("raw_stock_match", "28-0003", "ft")


def test_step4_sheet_skips_prefix_gate(cursor):
    r = lookup_material(cursor, "123-456-789", "HRS SH 10GA 18-0025")
    assert (r.status, r.matched_material) == ("raw_stock_match", "18-0025")
    assert not cursor.calls_matching("LIKE ?")


def test_step4_trailing_number_without_shape_code_is_not_raw_stock(cursor):
    assert lookup_material(cursor, JOB_PART, "MISC THING 28-0003").status == "not_found"


def test_uhmw_sheet_needs_review_with_candidate(cursor):
    r = lookup_material(cursor, JOB_PART, "UHMW SH .5 X 48 X 120 18-0100")
    assert (r.status, r.matched_material) == ("needs_review_uhmw", "18-0100")


def test_uhmw_needs_review_even_without_candidate(cursor):
    r = lookup_material(cursor, JOB_PART, "UHMW SH .5 X 48 X 120 18-9999")
    assert (r.status, r.matched_material) == ("needs_review_uhmw", None)


def test_uhmw_bar_is_ordinary_raw_stock(cursor):
    r = lookup_material(cursor, JOB_PART, "UHMW BR RD .75 28-0200")
    assert (r.status, r.matched_material) == ("raw_stock_match", "28-0200")


def test_lexan_needs_review(cursor):
    r = lookup_material(cursor, JOB_PART, "LEX SH .25 X 48 X 96 18-0300")
    assert (r.status, r.matched_material) == ("needs_review_lexan", "18-0300")


def test_step5_prefix_search_is_ambiguous_and_keeps_raw_stock_candidate(cursor):
    r = lookup_material(cursor, "123-456-789", "STEEL TU 1 X 1 28-0003")
    assert r.status == "ambiguous"
    assert r.searched_prefix == "123-456"
    assert [c.material_number for c in r.candidates] == ["123-456-001", "123-456-002", "28-0003"]
    assert [c.is_raw_stock for c in r.candidates] == [False, False, True]
    assert r.candidates[-1].description.endswith("(raw stock — unconfirmed)")


def test_step5_no_candidates_trusts_raw_stock(cursor):
    r = lookup_material(cursor, "999-888-777", "STEEL TU 1 X 1 28-0003")
    assert (r.status, r.matched_material) == ("raw_stock_match", "28-0003")


def test_step5_stops_at_two_segments(cursor):
    lookup_material(cursor, "999-888-777-666", "")
    prefixes = [c.params[0] for c in cursor.calls_matching("LIKE ?")]
    assert prefixes == ["999-888-777%", "999-888%"]


def test_step5_escapes_wildcards(cursor):
    lookup_material(cursor, "A_1-B%2-C", "")
    assert [c.params[0] for c in cursor.calls_matching("LIKE ?")] == ["A[_]1-B[%]2%"]


def test_not_found(cursor):
    r = lookup_material(cursor, "ABC", "")
    assert r.status == "not_found"
    assert r.matched_material is None and r.candidates == []
