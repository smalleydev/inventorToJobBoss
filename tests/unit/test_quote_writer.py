"""Characterization tests for quote_writer, using a fake cursor."""

import re

import pytest

import quote_writer as qw
from quote_writer import QuoteLine, write_quote
from tests.fakes import FakeCursor, material_table

GUID = re.compile(r"^\{[0-9a-f-]{36}\}$")


# --- Pure helpers -----------------------------------------------------------

def test_truncate():
    assert qw._truncate("x" * 60, 50) == "x" * 50
    assert qw._truncate("short", 50) == "short"
    assert qw._truncate("", 50) == ""
    assert qw._truncate(None, 50) is None


def test_custom_line_info():
    info = qw._custom_line_info(QuoteLine("MISC-1", 1, is_custom_line=True,
                                          description="Widget", ext_description="Long text"))
    assert info == {"description": "Widget", "ext_description": "Long text", "type": "M",
                    "pick_buy_indicator": "B", "vendor": None, "uofm": "ea",
                    "cost_uofm": "ea", "unit_cost": 0.0}


@pytest.mark.parametrize("line,expected", [
    (QuoteLine("M", 2, total_stock_length_in=480, material_used_in=290.125), (0.0, 290)),
    (QuoteLine("M", 2, total_stock_length_in=480), (0.0, 480)),  # fallback
    (QuoteLine("M", 1, cut_length_in=36.5), (0.0, 36.5)),
    (QuoteLine("M", 4), (4, None)),
    (QuoteLine("M", 4, cut_length_in=0, total_stock_length_in=0), (4, None)),
])
def test_line_quantity_and_part_length(line, expected):
    assert qw._line_quantity_and_part_length(line) == expected


# --- write_quote ------------------------------------------------------------

def jobboss(rfq_exists=False, existing_quotes=(), materials=None):
    """A fake cursor that answers every statement write_quote issues."""
    identities = iter(range(1000, 2000))

    def business_key(sql, params):
        m = re.search(r"SELECT (\w+) FROM (\w+) WHERE (\w+) = \?", sql)
        if not m or m.group(2) in ("RFQ", "Quote", "Material"):
            return None
        return [(f"{m.group(2)}#{params[0]}",)]

    cursor = FakeCursor()
    cursor.on("SELECT 1 FROM RFQ WHERE RFQ = ?", [{"x": 1}] if rfq_exists else [])
    cursor.on("SELECT Quote FROM Quote WHERE RFQ = ?", [{"Quote": q} for q in existing_quotes])
    cursor.on("SCOPE_IDENTITY()", lambda _p: [{"NewIdentity": next(identities)}])
    cursor.handlers.append(business_key)
    cursor.handlers.extend(material_table(materials or {
        "11-0087": {"Description": "HHCS 1/4-20 X 1 " + "X" * 60, "Standard_Cost": 0.5},
    }))
    return cursor


def statement_kinds(cursor):
    out = []
    for sql in cursor.statements():
        for kind in ("SET NOCOUNT", "SELECT 1 FROM RFQ", "INSERT INTO RFQ",
                     "SELECT Quote FROM Quote",
                     "INSERT INTO Quote_Req_Qty", "INSERT INTO Quote_Req", "INSERT INTO Quote_Qty",
                     "INSERT INTO Quote", "FROM Material", "SELECT Quote_Req_Qty FROM",
                     "SELECT Quote_Req FROM", "SELECT Quote_Qty_Key FROM"):
            if kind in sql:
                out.append(kind)
                break
        else:
            out.append(sql)
    return out


def test_write_quote_statement_sequence():
    cursor = jobboss()
    guid = write_quote(cursor, "28229A", "28229-01", "Frame", [QuoteLine("11-0087", 4)],
                       quoted_by="ENG")
    assert GUID.match(guid)
    assert statement_kinds(cursor) == [
        "SET NOCOUNT", "SELECT 1 FROM RFQ", "INSERT INTO RFQ", "SELECT Quote FROM Quote",
        "INSERT INTO Quote", "INSERT INTO Quote_Qty", "SELECT Quote_Qty_Key FROM",
        "FROM Material", "INSERT INTO Quote_Req", "SELECT Quote_Req FROM",
        "INSERT INTO Quote_Req_Qty", "SELECT Quote_Req_Qty FROM",
    ]
    assert cursor.connection.commits == 0  # caller owns the transaction


def test_rfq_insert_skipped_when_rfq_exists():
    cursor = jobboss(rfq_exists=True)
    write_quote(cursor, "28229A", "28229-01", "Frame", [])
    assert not cursor.calls_matching("INSERT INTO RFQ")


def test_quote_row_is_self_referencing():
    cursor = jobboss()
    guid = write_quote(cursor, "28229A", "28229-01", "Frame", [], quoted_by="ENG")
    [insert] = [c for c in cursor.calls if c.sql.startswith("INSERT INTO Quote (")]
    assert insert.params == (guid, guid, "Frame", "28229A", "ENG")


def test_existing_quote_refuses_duplicate():
    cursor = jobboss(existing_quotes=["{old}"])
    with pytest.raises(ValueError, match="already has 1 existing Quote"):
        write_quote(cursor, "28229A", "28229-01", "Frame", [QuoteLine("11-0087", 1)])
    assert not cursor.calls_matching("INSERT INTO Quote (")


def test_quote_req_params_for_material_line():
    cursor = jobboss()
    write_quote(cursor, "Q", "P", "D", [QuoteLine("11-0087", 4)])
    [req] = cursor.calls_matching("INSERT INTO Quote_Req (")
    p = req.params
    assert p[1] == "11-0087"          # Material
    assert p[3] == "H"                # Type (default)
    assert len(p[4]) == qw.DESCRIPTION_MAX_LEN
    assert p[8] == 4                  # Quantity_Per
    assert p[12] is None              # Part_Length
    [req_qty] = cursor.calls_matching("INSERT INTO Quote_Req_Qty")
    assert req_qty.params[0] == "Quote_Req#1001"
    assert req_qty.params[3] == "Quote_Qty#1000"
    assert req_qty.params[-1] == pytest.approx(4 * 0.5)  # Est_Total_Cost


def test_custom_line_is_not_looked_up():
    cursor = jobboss()
    write_quote(cursor, "Q", "P", "D", [QuoteLine("MISC-1", 1, is_custom_line=True,
                                                 description="Widget")])
    assert not cursor.calls_matching("FROM Material")
    [req] = cursor.calls_matching("INSERT INTO Quote_Req (")
    assert (req.params[1], req.params[3], req.params[4]) == ("MISC-1", "M", "Widget")


def test_unknown_material_raises():
    with pytest.raises(ValueError, match="not found in JobBOSS"):
        write_quote(jobboss(), "Q", "P", "D", [QuoteLine("NOPE", 1)])


def test_missing_identity_raises():
    cursor = jobboss()
    cursor.handlers.insert(0, lambda sql, _p: [] if "SCOPE_IDENTITY" in sql else None)
    with pytest.raises(RuntimeError, match="did not return an identity"):
        write_quote(cursor, "Q", "P", "D", [])
