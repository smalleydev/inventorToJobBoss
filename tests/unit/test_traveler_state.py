"""Characterization tests for traveler_state.compute_traveler_state.

Records current behavior rule by rule. Known bugs are xfail(strict)
tests of the CORRECT behavior; they flip to passing when fixed in the
bug-fix phase.
"""

import pytest

from traveler_state import SORT_ORDER, compute_traveler_state


def row(**fields):
    base = {"PartNumber": "123-456", "Material": "", "Category": "", "MatchStatus": "exact_part",
            "JobBossMaterial": "11-0087", "CutLengthIn": 0}
    base.update(fields)
    return base


# --- Rule 1: explicit override and ignored families -------------------------

def test_manually_ignored_always_wins():
    assert compute_traveler_state(row(MatchStatus="manually_ignored", PartNumber="NA")) == "Ignored"


def test_ignored_part_number_prefix():
    assert compute_traveler_state(row(PartNumber="10-1234")) == "Ignored"


def test_prefix_needs_its_hyphen():
    # "10-" must not catch "100-..."
    assert compute_traveler_state(row(PartNumber="100-1234")) == "Clean"


def test_ignored_vendor_number_prefix():
    r = row(PartNumber="NA", Material="VENDOR 10-0341", MatchStatus="exact_vendor")
    assert compute_traveler_state(r) == "Ignored"


def test_exception_list_applies_to_vendor_number():
    r = row(PartNumber="NA", Material="VENDOR 10-2101", MatchStatus="exact_vendor",
            JobBossMaterial="10-2101")
    assert compute_traveler_state(r) == "Clean"


@pytest.mark.parametrize("pn", ["10-2101", "10-6210", "10-2129", "10-1655", "10-5796",
                                " 10-2101 "])
def test_exception_list_applies_to_part_number(pn):
    # Regression: a duplicate rule-1c block used to ignore these anyway.
    assert compute_traveler_state(row(PartNumber=pn, JobBossMaterial="10-2101")) == "Clean"


def test_non_exception_in_family_still_ignored_with_exact_match():
    assert compute_traveler_state(row(PartNumber="10-2102", JobBossMaterial="10-2102")) == "Ignored"


# --- Rules 2-6 --------------------------------------------------------------

def test_custom_line():
    assert compute_traveler_state(row(MatchStatus="custom_line", JobBossMaterial="")) == "Custom"


def test_missing_part_number_needs_attention():
    assert compute_traveler_state(row(PartNumber="NA", Material="A36 STEEL")) == "Needs Attention"


def test_missing_part_number_is_fine_for_vendor_hardware():
    r = row(PartNumber="na", Material="VENDOR 11-0341", MatchStatus="exact_vendor")
    assert compute_traveler_state(r) == "Clean"


@pytest.mark.parametrize("status", ["needs_review_uhmw", "needs_review_lexan"])
def test_special_review_beats_sheet_ignore(status):
    assert compute_traveler_state(row(MatchStatus=status, Category="SHEET")) == "Needs Attention"


@pytest.mark.parametrize("category", ["SHEET", "PLATE"])
def test_sheet_plate_ignored_unless_exact(category):
    r = row(MatchStatus="raw_stock_match", Category=category)
    assert compute_traveler_state(r) == "Ignored"
    assert compute_traveler_state(row(MatchStatus="not_found", Category=category,
                                      JobBossMaterial=None)) == "Ignored"
    assert compute_traveler_state(row(MatchStatus="exact_part", Category=category)) == "Clean"


@pytest.mark.parametrize("status", ["ambiguous", "not_found", "new_material_needed"])
def test_unresolved_needs_attention(status):
    assert compute_traveler_state(row(MatchStatus=status)) == "Needs Attention"


# --- Rule 7: Needs Length ---------------------------------------------------

@pytest.mark.parametrize("uofm", ["ft", " FT ", "in", "FEET", "inch"])
def test_linear_uofm_needs_length(uofm):
    assert compute_traveler_state(row(JobBossUofM=uofm, CutLengthIn=0)) == "Needs Length"


def test_linear_uofm_with_length_is_clean():
    assert compute_traveler_state(row(JobBossUofM="ft", CutLengthIn=12.5)) == "Clean"


def test_missing_cut_length_key_counts_as_zero():
    r = row(JobBossUofM="ft")
    del r["CutLengthIn"]
    assert compute_traveler_state(r) == "Needs Length"


def test_none_cut_length_does_not_need_length():
    """Intended: None means "no single length applies" (it's what
    _combine_rows puts on nested lines), not "length missing". The
    add-in writes CutLengthIn as a Double, so a missing length arrives
    as 0, which is what triggers Needs Length."""
    assert compute_traveler_state(row(JobBossUofM="ft", CutLengthIn=None)) == "Clean"


def test_raw_stock_linear_category_needs_length_without_uofm():
    r = row(MatchStatus="raw_stock_match", Category="TUBE", JobBossUofM="ea")
    assert compute_traveler_state(r) == "Needs Length"


def test_exact_match_linear_category_does_not_need_length_by_category():
    r = row(MatchStatus="exact_part", Category="TUBE", JobBossUofM="ea")
    assert compute_traveler_state(r) == "Clean"


def test_manual_raw_stock_needs_length():
    r = row(MatchStatus="resolved_manual_raw_stock", Category="ANGLE")
    assert compute_traveler_state(r) == "Needs Length"


# --- Rule 8 -----------------------------------------------------------------

@pytest.mark.parametrize("status", ["resolved_manual", "resolved_manual_raw_stock"])
def test_manual_resolution_is_attended(status):
    assert compute_traveler_state(row(MatchStatus=status, CutLengthIn=10)) == "Attended"


@pytest.mark.parametrize("status", ["exact_part", "exact_vendor", "jb_reference",
                                    "raw_stock_match"])
def test_auto_resolution_is_clean(status):
    assert compute_traveler_state(row(MatchStatus=status)) == "Clean"


def test_no_material_is_never_clean():
    assert compute_traveler_state(row(JobBossMaterial="")) == "Needs Attention"


def test_sort_order_puts_attention_first():
    assert sorted(SORT_ORDER, key=SORT_ORDER.get) == [
        "Needs Attention", "Needs Length", "Ignored", "Attended", "Clean", "Custom",
    ]
