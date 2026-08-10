"""
Writes one finalized BOM (from JL Check's approved export) into JobBOSS
as a new Quote.

CONFIRMED against a SQL Profiler trace of a real manual quote creation
in the JobBOSS client:

  1. INSERT INTO RFQ — a real row in the RFQ master table, RFQ = the
     quote number. Required for the quote to be discoverable via the
     client's Quote ID lookup.
  2. INSERT INTO Quote — ONE self-referencing row (Quote = Top_Lvl_Quote),
     Type='Regular', Part_Number=NULL, RFQ=<quote number>, Line='001'.
     Material lines attach directly to this one row.
  3. Quote_Req rows, one per material line — includes Vendor
     (Material.Primary_Vendor).
  4. Quote_Qty (one pricing tier).
  5. Quote_Req_Qty, one per material line.

Key generation: Quote_Qty, Quote_Req, and Quote_Req_Qty all have
AFTER INSERT triggers that call JobBOSS's own p_GetNextKey procedure
whenever the business-key column is left NULL on insert.

Field derivation: Description, cost, UofM, Type, and Vendor are looked
up from Material — the same source the JobBOSS client reads from.

TWO KINDS OF LINE, per JL Check's stock-nesting combine step:
  - Single-length line: cut_length_in is set, total_stock_length_in is
    None. Part_Length is that one length, in inches. Quantity_Per and
    Est_Qty are deliberately left at 0 — the shop doesn't use those
    fields for material lines that carry a length, only Part_Length.
    (Hardware/vendor rows are single-length lines with cut_length_in
    always None — "no length" rather than "one length" — and DO get a
    real Quantity_Per, since those are plain piece-count items.)
  - Nested combined line: total_stock_length_in is set (from stock
    nesting — multiple cut lengths of one material packed into standard
    sticks), cut_length_in is None. Part_Length is the TRUE material
    consumed (material_used_in — sum of every piece + kerf, not rounded
    up to whole sticks), rounded to the nearest inch. Quantity_Per and
    Est_Qty are left at 0, same reasoning as above.

Quantity_Per_Basis is kept at 'I' (Individual) for every line — see
prior notes; JobBOSS's own 'B' (Bar) basis appears tied to its own
bar-nesting calculator, which this tool deliberately does not use,
since nesting/kerf math is computed independently here instead.

Everything happens inside ONE transaction, supplied by the caller.

CUSTOM LINES: a QuoteLine with is_custom_line=True skips the Material
lookup entirely — jobboss_material still gets written to
Quote_Req.Material as free text (CONFIRMED via
check_quote_req_schema.py: no FK back to Material), but
description/ext_description come from the engineer's own typed fields.
Type='M'/Pick_Buy_Indicator='B' are CONFIRMED via a Profiler trace of a
real manually-entered Misc line in the JobBOSS client (2026-08-05), as
is Trade_Date always being set to GETDATE() — previously left NULL on
every line, now written on all of them, matched or custom.

EXTENDED DESCRIPTION: check_quote_req_schema.py CONFIRMED Quote_Req has
no Ext_Description column at all — so ext_description (whether pulled
from Material for a matched line, or typed by the engineer for a custom
one) is written to Note_Text instead (TEXT, no length limit). This also
lines up with a real ECI forum complaint about Misc-material notes not
propagating to external RFQ/PO documents — Note_Text being an INTERNAL-
only field is a known, reported behavior, not a guess.

Quote_Req.Description is varchar(50) — truncated before insert (see
_truncate) since a long engineer-typed description would otherwise
throw a SQL string-truncation error rather than a clean Python one.
"""

import uuid
from dataclasses import dataclass

# Quote_Req.Description is varchar(50) — confirmed via
# check_quote_req_schema.py. Truncating here gives a predictable,
# silent trim instead of letting the driver throw a string-truncation
# error at insert time.
DESCRIPTION_MAX_LEN = 50


def _truncate(value: str, max_len: int) -> str:
    return value[:max_len] if value else value


@dataclass
class QuoteLine:
    """
    One material line, already resolved and combined by JL Check.

    Exactly one of (cut_length_in, total_stock_length_in) should carry
    a real value for a line that needs one — both None means a plain
    piece-count line (hardware, vendor items).

    material_used_in: for a nested combined line, the true material
    consumed (sum of every piece + kerf) — distinct from
    total_stock_length_in, which rounds up to whole sticks for
    purchasing. Written to Part_Length instead of the stock total, so
    the quote reflects actual usage (useful for judging whether scrap
    or partial stock on hand already covers the need).

    CUSTOM LINES (is_custom_line=True): added via JL Check's "Add as
    Custom Line" — engineer-typed ID/Description/Extended Description,
    never matched against Material at all. jobboss_material still holds
    the engineer's typed ID and still gets written to Quote_Req.Material
    as free text (confirmed by check_quote_req_schema.py to be
    unconstrained — no FK back to Material), but description/
    ext_description are used directly instead of looked up, and
    _lookup_material is never called for these — there's nothing in
    Material to look up.
    """
    jobboss_material: str
    quantity: float
    cut_length_in: float | None = None
    total_stock_length_in: float | None = None
    material_used_in: float | None = None
    is_custom_line: bool = False
    description: str | None = None
    ext_description: str | None = None


def _lookup_material(cursor, material_number: str) -> dict:
    """Pulls the fields JobBOSS's own client would auto-fill when a
    Material ID is entered on a quote line."""
    cursor.execute(
        """
        SELECT Description, Ext_Description, Type, Pick_Buy_Indicator,
               Primary_Vendor, Stocked_UofM, Cost_UofM, Standard_Cost
        FROM Material
        WHERE Material = ?
        """,
        material_number,
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"Material '{material_number}' not found in JobBOSS.")

    return {
        "description": row.Description or "",
        "ext_description": row.Ext_Description or "",
        "type": row.Type or "H",
        "pick_buy_indicator": row.Pick_Buy_Indicator or "P",
        "vendor": row.Primary_Vendor,
        "uofm": row.Stocked_UofM or "ea",
        "cost_uofm": row.Cost_UofM or "ea",
        "unit_cost": row.Standard_Cost or 0.0,
    }


def _custom_line_info(line: "QuoteLine") -> dict:
    """
    Builds the same shape _lookup_material returns, but from the
    engineer's own typed fields instead of a Material record — there's
    no Material row to look up for a custom line.

    type='M' and pick_buy_indicator='B' are CONFIRMED via a Profiler
    trace of a real manually-entered Misc line in the JobBOSS client
    (2026-08-05) — not guesses anymore. unit_cost is 0.0 since JL Check
    doesn't currently capture a price for these — the estimator can
    price it directly in the JobBOSS client after the quote lands.
    """
    return {
        "description": line.description or "",
        "ext_description": line.ext_description or "",
        "type": "M",
        "pick_buy_indicator": "B",
        "vendor": None,
        "uofm": "ea",
        "cost_uofm": "ea",
        "unit_cost": 0.0,
    }


def _line_quantity_and_part_length(line: "QuoteLine") -> tuple[float, float | None]:
    """
    Returns (quantity_per_value, part_length_value) for one line,
    depending on which kind of line it is:
      - Nested combined line (total_stock_length_in set): quantity is
        deliberately 0 — the shop doesn't use Quantity_Per/Est_Qty for
        these lines, only Part_Length. Part_Length is the true material
        CONSUMED (material_used_in), rounded to the nearest inch — not
        the rounded-up-to-whole-sticks order amount.
      - Single-length line (cut_length_in set): quantity is also 0, for
        the same reason. Part_Length is the one length, in inches.
      - Plain piece-count line (neither set): quantity is the real
        piece count (this is the normal "each" quantity, unrelated to
        the length-line convention above); no Part_Length.
    """
    if line.total_stock_length_in not in (None, 0, 0.0):
        part_length = (
            round(line.material_used_in)
            if line.material_used_in not in (None, 0, 0.0)
            else round(line.total_stock_length_in)  # fallback if not supplied
        )
        return 0.0, part_length

    if line.cut_length_in not in (None, 0, 0.0):
        return 0.0, line.cut_length_in

    return line.quantity, None


def _insert_and_get_generated_key(cursor, insert_sql: str, params: tuple,
                                  table: str, identity_col: str, business_key_col: str) -> int:
    """
    Runs an INSERT that leaves `business_key_col` NULL, letting the
    table's AFTER INSERT trigger assign it via p_GetNextKey. Captures
    the row via SCOPE_IDENTITY() and re-queries for the trigger-
    populated business key.
    """
    cursor.execute(insert_sql + "; SELECT SCOPE_IDENTITY() AS NewIdentity", *params)
    identity_row = cursor.fetchone()
    if identity_row is None or identity_row.NewIdentity is None:
        raise RuntimeError(f"Insert into {table} did not return an identity value.")
    identity_value = int(identity_row.NewIdentity)

    cursor.execute(
        f"SELECT {business_key_col} FROM {table} WHERE {identity_col} = ?",
        identity_value,
    )
    key_row = cursor.fetchone()
    if key_row is None or key_row[0] is None:
        raise RuntimeError(
            f"{table}.{business_key_col} was not populated by its insert "
            f"trigger for {identity_col} = {identity_value}."
        )
    return key_row[0]


def write_quote(cursor, quote_number: str, part_number: str,
                description: str, lines: list[QuoteLine],
                quoted_by: str = "LSTRAIN") -> str:
    """
    Writes a full quote: an RFQ master row, a single self-referencing
    Quote row, and its material lines. `cursor`'s connection must have
    autocommit disabled — caller commits (or rolls back) after this
    returns.

    Returns the new Quote's GUID.
    """
    cursor.execute("SET NOCOUNT ON")

    # --- 1. RFQ master row --------------------------------------------------
    # A retry of a quote number that already succeeded once (or partially
    # got as far as this step before failing) will find the RFQ row
    # already exists — that's expected and fine, not an error: real
    # JobBOSS quotes can have multiple Quote records sharing one RFQ, so
    # reusing an existing row is correct behavior, not a workaround.
    cursor.execute("SELECT 1 FROM RFQ WHERE RFQ = ?", quote_number)
    rfq_already_exists = cursor.fetchone() is not None

    if not rfq_already_exists:
        cursor.execute(
            """
            INSERT INTO RFQ
                (RFQ, Quote_Date, Commission_Pct, Note_Text, Comments,
                 Status_Date, RFQ_Date, Last_Updated, Trade_Currency,
                 Fixed_Rate, Trade_Date, Currency_Conv_Rate, Certs_Required,
                 Quoted_By, Submitted_Date, Expiration_Date, Status,
                 Source, Win_Probability)
            VALUES
                (?, GETDATE(), 0, '', '',
                 GETDATE(), GETDATE(), GETDATE(), 1,
                 1, GETDATE(), 1.0, 0,
                 ?, GETDATE(), DATEADD(day, 14, GETDATE()), 'Active',
                 'System', 80)
            """,
            quote_number, quoted_by,
        )

    # --- 2. Single Quote row (self-referencing) ------------------------------
    quote_guid = f"{{{uuid.uuid4()}}}"

    cursor.execute(
        """
        INSERT INTO Quote (Quote, Top_Lvl_Quote, Description,
                            Type, Status, Status_Date, Assembly_Level,
                            RFQ, Line, Quoted_By, Win_Probability,
                            Ext_Description, Note_Text, Comment, Profit_Markup)
        VALUES (?, ?, ?, 'Regular', 'Active', GETDATE(), 0,
                ?, '001', ?, 80, '', '', '', 'M')
        """,
        quote_guid, quote_guid, description, quote_number, quoted_by,
    )

    # --- 3. Quote_Qty (one pricing tier) -----------------------------------
    quote_qty_key = _insert_and_get_generated_key(
        cursor,
        """
        INSERT INTO Quote_Qty (Quote, Quote_Qty, Yield_Pct, Make_Quantity)
        VALUES (?, 1, 100.0, 1)
        """,
        (quote_guid,),
        table="Quote_Qty",
        identity_col="Quote_QtyKey",
        business_key_col="Quote_Qty_Key",
    )

    # --- 4. One Quote_Req + Quote_Req_Qty pair per material line -----------
    for line in lines:
        material_info = (
            _custom_line_info(line) if line.is_custom_line
            else _lookup_material(cursor, line.jobboss_material)
        )

        converted_qty, part_length = _line_quantity_and_part_length(line)
        basis = "I"

        quote_req_key = _insert_and_get_generated_key(
            cursor,
            """
            INSERT INTO Quote_Req
                (Quote, Material, Vendor, Type, Description, Note_Text,
                 Pick_Buy_Indicator, Quantity_Per_Basis, Quantity_Per, UofM,
                 Cost_UofM, Est_Unit_Cost, Part_Length, Rounded, Fixed_Rate,
                 Trade_Date, Currency_Conv_Rate, Trade_Currency, Cost_Unit_Conv,
                 Quantity_Multiplier, Certs_Required, Affects_Schedule)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, GETDATE(), 1.0, 1, 1.0, 1.0, 0, 0)
            """,
            (
                quote_guid,
                line.jobboss_material,
                material_info["vendor"],
                material_info["type"],
                _truncate(material_info["description"], DESCRIPTION_MAX_LEN),
                material_info["ext_description"] or None,
                material_info["pick_buy_indicator"],
                basis,
                converted_qty,
                material_info["uofm"],
                material_info["cost_uofm"],
                material_info["unit_cost"],
                part_length,
            ),
            table="Quote_Req",
            identity_col="Quote_ReqKey",
            business_key_col="Quote_Req",
        )

        est_total_cost = converted_qty * material_info["unit_cost"]

        _insert_and_get_generated_key(
            cursor,
            """
            INSERT INTO Quote_Req_Qty
                (Quote_Req, Quote, Material, Quote_Qty_Key,
                 Make_Quantity, Est_Qty, Est_Unit_Cost, Est_Total_Cost)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                quote_req_key, quote_guid, line.jobboss_material,
                quote_qty_key, converted_qty, material_info["unit_cost"],
                est_total_cost,
            ),
            table="Quote_Req_Qty",
            identity_col="Quote_Req_QtyKey",
            business_key_col="Quote_Req_Qty",
        )

    return quote_guid