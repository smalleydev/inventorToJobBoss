"""Characterization tests for stock_nesting (FFD bin packing with kerf)."""

import pytest

from stock_nesting import KERF_ALLOWANCE_IN, NestingResult, expand_pieces, nest_pieces


def test_kerf_default():
    assert KERF_ALLOWANCE_IN == 0.125


def test_empty_input():
    assert nest_pieces([], 144) == NestingResult(0, 0, 144, 0.0, 0.0)


def test_pieces_share_one_stick():
    r = nest_pieces([20, 20, 20], 144)
    assert r.sticks_needed == 1
    assert r.total_piece_count == 3
    assert r.total_stock_length_in == 144
    assert r.total_material_used_in == pytest.approx(60.375)  # 3 x (20 + kerf)


def test_first_fit_decreasing():
    # 100.125 opens stick 1; 50.125 doesn't fit its 43.875 leftover, opens
    # stick 2; the second 50.125 also lands on stick 2.
    r = nest_pieces([50, 100, 50], 144)
    assert r.sticks_needed == 2
    assert r.total_stock_length_in == 288
    # every stick but the last charged in full, plus the last stick's use
    assert r.total_material_used_in == pytest.approx(144 + 100.25)


def test_piece_exactly_filling_stick_fits():
    assert nest_pieces([143.875], 144).sticks_needed == 1


def test_oversized_piece_raises():
    with pytest.raises(ValueError, match="exceeds the stick length"):
        nest_pieces([144], 144)


def test_custom_kerf():
    r = nest_pieces([72, 72], 144, kerf_in=0)
    assert r.sticks_needed == 1
    assert r.total_material_used_in == pytest.approx(144)


def test_expand_pieces():
    rows = [
        {"CutLengthIn": 20, "Quantity": 3},
        {"CutLengthIn": None, "Quantity": 2},   # no length: skipped
        {"CutLengthIn": 5, "Quantity": 0},      # no quantity: skipped
        {"CutLengthIn": 7.5, "Quantity": None},  # None quantity: skipped
        {"CutLengthIn": "7.5", "Quantity": "2"},  # strings are coerced
    ]
    assert expand_pieces(rows) == [20.0, 20.0, 20.0, 7.5, 7.5]
