"""Characterization tests for main._combine_rows and friends.

These live in main.py today, which imports PySide6, so the module is
skipped where PySide6 isn't installed. They move to a Qt-free module in
the JL Check refactor phase.
"""

import pytest

pytest.importorskip("PySide6")

import main  # noqa: E402
from tests.fakes import FakeCursor, material_table  # noqa: E402


@pytest.fixture
def cursor():
    return FakeCursor(material_table({
        "28-0003": {"IS_Length": 20, "Stocked_UofM": "ft"},   # 240 in
        "28-0004": {"IS_Length": 240, "Stocked_UofM": "in"},
        "28-0005": {"IS_Length": None},
    }))


def hw(pn, material, qty, **extra):
    return {"PartNumber": pn, "Description": f"desc {pn}", "Material": "", "Category": "",
            "JobBossMaterial": material, "Quantity": qty, "MatchStatus": "exact_part",
            "TravelerState": "Clean", **extra}


def tube(pn, material, qty, length, uofm="ft"):
    return hw(pn, material, qty, Category="TUBE", CutLengthIn=length, JobBossUofM=uofm,
              MatchStatus="raw_stock_match")


@pytest.mark.parametrize("value,uofm,expected", [
    (20, "ft", 240), (20, " FT ", 240), (20, "in", 20), (20, None, 20), (20, "ea", 20),
])
def test_stick_length_to_inches(value, uofm, expected):
    assert main._stick_length_to_inches(value, uofm) == expected


def test_single_row_passes_through_as_copy(cursor):
    source = hw("P1", "11-0087", 4)
    [out] = main._combine_rows([source], cursor)
    assert out["SourcePartNumbers"] == "P1"
    assert out["CutList"] is None
    assert out["Quantity"] == 4
    assert "SourcePartNumbers" not in source  # input not mutated


def test_same_material_rows_are_summed(cursor):
    [out] = main._combine_rows([hw("P1", "11-0087", 4), hw("P2", "11-0087", 2)], cursor)
    assert out["PartNumber"] == "11-0087"
    assert out["JobBossMaterial"] == "11-0087"
    assert out["Quantity"] == 6
    assert out["Description"] == "Combined (2 items) — desc P1"
    assert out["SourcePartNumbers"] == "P1, P2"
    assert out["CutLengthIn"] is None


def test_unmatched_rows_group_by_part_number(cursor):
    out = main._combine_rows([hw("P1", None, 1), hw("P2", None, 1)], cursor)
    assert [r["SourcePartNumbers"] for r in out] == ["P1", "P2"]


def test_simple_rows_with_different_lengths_stay_separate(cursor):
    rows = [hw("P1", "11-0087", 1, CutLengthIn=10), hw("P2", "11-0087", 1, CutLengthIn=12)]
    assert [r["CutLengthIn"] for r in main._combine_rows(rows, cursor)] == [10, 12]


def test_linear_row_without_length_is_simple(cursor):
    [out] = main._combine_rows([tube("T1", "28-0003", 2, None)], cursor)
    assert out["CutList"] is None
    assert not cursor.calls_matching("IS_Length")


def test_nested_rows_combine_into_one_line(cursor):
    rows = [tube("T1", "28-0003", 2, 100), tube("T2", "28-0003", 1, 50)]
    [out] = main._combine_rows(rows, cursor)
    # pieces 100.125, 100.125 share stick 1 (240); 50.125 opens stick 2
    assert out["Quantity"] == 2
    assert out["Description"] == "Nested (3 cuts, 2 sticks) — desc T1"
    assert out["CutLengthIn"] is None
    assert out["CutList"] == '2x @ 100", 1x @ 50"'
    assert out["SourcePartNumbers"] == "T1, T2"
    assert out["TotalStockLengthIn"] == 480
    assert out["MaterialUsedIn"] == pytest.approx(240 + 50.125)


def test_stick_length_in_inches_is_not_converted(cursor):
    [out] = main._combine_rows([tube("T1", "28-0004", 1, 100, uofm="in")], cursor)
    assert out["TotalStockLengthIn"] == 240


def test_simple_rows_come_before_nested(cursor):
    out = main._combine_rows([tube("T1", "28-0003", 1, 10), hw("P1", "11-0087", 1)], cursor)
    assert [r["PartNumber"] for r in out] == ["P1", "28-0003"]


def test_missing_stick_length_raises(cursor):
    with pytest.raises(ValueError, match="no IS_Length"):
        main._combine_rows([tube("T1", "28-0005", 1, 10)], cursor)


def test_zero_quantity_nested_group_raises(cursor):
    with pytest.raises(ValueError, match="no material-consumed total"):
        main._combine_rows([tube("T1", "28-0003", 0, 10)], cursor)


def test_to_export_row_projects_export_fields():
    out = main._to_export_row({"PartNumber": "P1", "_orig_index": 3, "Children": []})
    assert tuple(out) == main.EXPORT_FIELDS
    assert out["PartNumber"] == "P1"
    assert out["Quantity"] is None
