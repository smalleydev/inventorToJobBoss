"""
Manual test harness for write_quote(). Loads one approved JSON export
(from JL Check's Finalize button) and writes it as a real quote into
TESTPROD, inside a transaction that's only committed if you explicitly
confirm — so a bad write can be caught and rolled back before it sticks.

Usage:
    python test_write_quote.py path\to\28229-01A.json
"""

import json
import sys

from db import get_connection
from quote_writer import QuoteLine, write_quote


def load_export(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_lines(payload: dict) -> list[QuoteLine]:
    lines = []
    for row in payload["Rows"]:
        is_custom = row.get("MatchStatus") == "custom_line"
        lines.append(QuoteLine(
            jobboss_material=row.get("JobBossMaterial", ""),
            quantity=row.get("Quantity", 0) or 0,
            cut_length_in=row.get("CutLengthIn"),
            total_stock_length_in=row.get("TotalStockLengthIn"),
            material_used_in=row.get("MaterialUsedIn"),
            is_custom_line=is_custom,
            description=row.get("Description") if is_custom else None,
            ext_description=row.get("ExtDescription") if is_custom else None,
        ))
    return lines


def verify_in_search_view(cursor, quote_number: str) -> None:
    """
    Checks whether the newly written quote is actually visible through
    vw_top_lvl_quotes — the view that backs both the Quote Entry search
    grid and direct Quote ID entry.
    """
    cursor.execute("SELECT * FROM vw_top_lvl_quotes WHERE RFQ = ?", quote_number)
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    print(f"\n=== vw_top_lvl_quotes check for RFQ = '{quote_number}' ===")
    if not rows:
        print("  NOT FOUND — this quote will likely be invisible in the JobBOSS client.")
    else:
        print(f"  FOUND — {len(rows)} row(s):")
        for row in rows:
            print("   ", dict(zip(columns, row)))


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_write_quote.py path\\to\\export.json")
        sys.exit(1)

    payload = load_export(sys.argv[1])
    quote_number = payload["QuoteNumber"].upper()
    lines = build_lines(payload)

    print(f"Quote number: {quote_number}")
    print(f"Line count:   {len(lines)}")
    for line in lines:
        print(f"  {line.jobboss_material:15} qty={line.quantity:<8} "
              f"len={line.cut_length_in} total_stock={line.total_stock_length_in}")

    confirm = input("\nWrite this to TESTPROD as a real quote? (yes/no): ")
    if confirm.strip().lower() != "yes":
        print("Aborted, nothing written.")
        return

    conn = get_connection()
    conn.autocommit = False
    cursor = conn.cursor()

    try:
        quote_guid = write_quote(
            cursor,
            quote_number=quote_number,
            part_number=quote_number,
            description=f"Imported from {payload.get('SourceFile', '')}",
            lines=lines,
        )

        print(f"\nWrote quote. GUID: {quote_guid}")
        final_confirm = input("Commit this write? (yes/no): ")

        if final_confirm.strip().lower() == "yes":
            conn.commit()
            print("Committed.")
            verify_in_search_view(cursor, quote_number)
        else:
            conn.rollback()
            print("Rolled back — nothing was written.")

    except Exception as exc:
        conn.rollback()
        print(f"\nERROR — rolled back, nothing was written.\n{exc}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()