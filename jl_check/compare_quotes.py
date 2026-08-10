"""
compare_quotes.py — read-only diff between two JobBOSS quotes' material
lines (Quote_Req), keyed by Material. Shows lines only in one quote, and
lines present in both but with a different quantity or length.

Usage:
    python compare_quotes.py <RFQ_A> <RFQ_B>

Compares whichever Quote row has Line='001' under each RFQ — the
top-level quote line, the same one write_quote() creates. Doesn't walk
Bill_Of_Quotes sub-assemblies; extend fetch_quote_lines() if this ever
needs to diff multi-level quotes.
"""

import re
import sys
from dataclasses import dataclass

from db import get_connection

# Job-specific part numbers are job-number-prefixed (e.g. "28179-05-012",
# or "28179-05-012_MIR" for a mirrored part) — the job number is always
# the leading 5 digits. That means the SAME physical part can never
# match on raw Material across two different jobs; comparing job A's
# "28179-05-012" against job B's "28255-05-012" needs the job number
# stripped off both sides first. Real JobBOSS material IDs and custom
# lines don't follow this shape and are left untouched.
JOB_SPECIFIC_PATTERN = re.compile(r"^\d{5}-\d+-\d+")


def is_job_specific(material: str) -> bool:
    return bool(JOB_SPECIFIC_PATTERN.match(material))


def comparison_key(material: str) -> str:
    """The key two lines are matched on. Job-specific numbers drop their
    leading 5-digit job number + hyphen (e.g. "28179-05-012_MIR" ->
    "05-012_MIR"), so the same part lines up across jobs. Everything
    else compares on the raw Material value, unchanged."""
    if is_job_specific(material):
        return material.split("-", 1)[1]
    return material


@dataclass
class QuoteLineRow:
    material: str
    description: str
    quantity: float
    part_length: float | None
    pick_buy: str


def fetch_quote_lines(cursor, rfq_number: str) -> dict[str, list[QuoteLineRow]]:
    """Returns Quote_Req lines for the top-level (Line='001') quote under
    `rfq_number`, keyed by comparison_key(Material) (see above) rather
    than the raw Material value, so job-specific parts line up across
    jobs. Value is a list rather than a single row — nothing stops the
    same key appearing on more than one line in a quote, and collapsing
    that silently would hide a real difference between two quotes."""
    cursor.execute(
        "SELECT Quote FROM Quote WHERE RFQ = ? AND Line = '001'",
        rfq_number,
    )
    quote_row = cursor.fetchone()
    if not quote_row:
        raise ValueError(f"No top-level quote found for RFQ '{rfq_number}'")
    quote_guid = quote_row.Quote

    cursor.execute(
        """
        SELECT qr.Material, qr.Description, qr.Part_Length,
               qr.Pick_Buy_Indicator, qrq.Est_Qty
        FROM Quote_Req qr
        JOIN Quote_Req_Qty qrq
            ON qrq.Quote_Req = qr.Quote_Req AND qrq.Quote = qr.Quote
        WHERE qr.Quote = ?
        ORDER BY qr.Material
        """,
        quote_guid,
    )

    lines: dict[str, list[QuoteLineRow]] = {}
    for row in cursor.fetchall():
        lines.setdefault(comparison_key(row.Material), []).append(QuoteLineRow(
            material=row.Material,
            description=row.Description,
            quantity=row.Est_Qty,
            part_length=row.Part_Length,
            pick_buy=row.Pick_Buy_Indicator,
        ))
    return lines


def _fmt_len(value) -> str:
    return "—" if value in (None, 0, 0.0) else f'{value}"'


def _fmt_qty(value) -> str:
    """Quantities computed via stock nesting can carry float noise
    (e.g. 41.91639999999999) — round for display only, comparisons
    still use the raw value via _normalize_qty."""
    return f"{round(float(value), 4):g}"


def _normalize_length(value) -> float:
    """Collapse None / 0 / Decimal(0) — all of which display as "—" via
    _fmt_len — down to one canonical value for comparison purposes.
    Part_Length comes back from SQL Server as Decimal; a line with
    NULL in one quote and Decimal('0.00') in the other both mean "no
    length" but are NOT equal under Python's own `!=`, which was
    flagging every such pair as a false "Changed" line even though
    nothing about the line actually differs."""
    return 0.0 if value in (None, 0, 0.0) else float(value)


def _normalize_qty(value) -> float:
    """Same reasoning as _normalize_length, applied to quantity — cheap
    insurance against the same None-vs-Decimal(0) mismatch."""
    return 0.0 if value in (None, 0, 0.0) else float(value)


def compare(rfq_a: str, rfq_b: str) -> None:
    conn = get_connection()
    cursor = conn.cursor()

    lines_a = fetch_quote_lines(cursor, rfq_a)
    lines_b = fetch_quote_lines(cursor, rfq_b)

    conn.close()

    only_a, only_b, changed, unchanged = [], [], [], []

    for key in sorted(set(lines_a) | set(lines_b)):
        rows_a = lines_a.get(key)
        rows_b = lines_b.get(key)

        if rows_a and not rows_b:
            only_a.append((key, rows_a))
            continue
        if rows_b and not rows_a:
            only_b.append((key, rows_b))
            continue

        # Present in both — compare totals. Summing quantity covers the
        # rare case of one key split across multiple lines; length is
        # taken off the first row (a split line should share one
        # length, so a mismatch there is itself worth surfacing as a
        # difference rather than silently picking one).
        qty_a = sum(r.quantity for r in rows_a)
        qty_b = sum(r.quantity for r in rows_b)
        len_a = rows_a[0].part_length
        len_b = rows_b[0].part_length

        if _normalize_qty(qty_a) != _normalize_qty(qty_b) or _normalize_length(len_a) != _normalize_length(len_b):
            changed.append((key, rows_a[0], qty_a, len_a, rows_b[0], qty_b, len_b))
        else:
            unchanged.append((key, rows_a[0], rows_b[0]))

    print(f"Comparing RFQ '{rfq_a}' vs RFQ '{rfq_b}'")
    print("=" * 70)

    print(f"\nOnly in {rfq_a} ({len(only_a)}):")
    for key, rows in only_a:
        r = rows[0]
        print(f"  {r.material:15} qty={_fmt_qty(sum(x.quantity for x in rows)):<8} "
              f"len={_fmt_len(r.part_length):<8} {r.description}")

    print(f"\nOnly in {rfq_b} ({len(only_b)}):")
    for key, rows in only_b:
        r = rows[0]
        print(f"  {r.material:15} qty={_fmt_qty(sum(x.quantity for x in rows)):<8} "
              f"len={_fmt_len(r.part_length):<8} {r.description}")

    print(f"\nChanged ({len(changed)}):")
    for key, ra, qty_a, len_a, rb, qty_b, len_b in changed:
        # Job-specific parts can carry different raw Material values on
        # each side even though they matched on the normalized key
        # (e.g. "28179-05-012" vs "28255-05-012") — show both so it's
        # obvious this is the same part across two different jobs, not
        # a typo.
        label = ra.material if ra.material == rb.material else f"{ra.material}  (== {rb.material})"
        print(f"  {label:15} {ra.description}")
        if _normalize_qty(qty_a) != _normalize_qty(qty_b):
            print(f"      qty:    {_fmt_qty(qty_a)}  ->  {_fmt_qty(qty_b)}")
        if _normalize_length(len_a) != _normalize_length(len_b):
            print(f"      length: {_fmt_len(len_a)}  ->  {_fmt_len(len_b)}")

    print(f"\nUnchanged: {len(unchanged)} line(s)")

    print("\n" + "=" * 70)
    print(f"Summary: {len(only_a)} removed, {len(only_b)} added, "
          f"{len(changed)} changed, {len(unchanged)} unchanged")


def main():
    if len(sys.argv) < 3:
        print("Usage: python compare_quotes.py <RFQ_A> <RFQ_B>")
        sys.exit(1)
    compare(sys.argv[1].upper(), sys.argv[2].upper())


if __name__ == "__main__":
    main()