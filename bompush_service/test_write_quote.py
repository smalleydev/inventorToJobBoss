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
        # Same optional-kwarg pattern as watcher_service.py: only pass
        # quoted_by through when the export actually has one, so an
        # older test file with no QuotedBy still falls back to
        # write_quote()'s own default instead of passing None explicitly.
        quoted_by_kwargs = {}
        if payload.get("QuotedBy"):
            quoted_by_kwargs["quoted_by"] = payload["QuotedBy"]

        quote_guid = write_quote(
            cursor,
            quote_number=quote_number,
            part_number=quote_number,
            description=f"Imported from {payload.get('SourceFile', '')}",
            lines=lines,
            **quoted_by_kwargs,
        )

        print(f"\nWrote quote. GUID: {quote_guid}")
        final_confirm = input("Commit this write? (yes/no): ")

        if final_confirm.strip().lower() == "yes":
            conn.commit()
            print("Committed.")
            verify_in_search_view(cursor, quote_number)

            test_guard = input(
                "\nTest overwrite guard now by attempting a second write "
                f"to '{quote_number}'? (yes/no): "
            )
            if test_guard.strip().lower() == "yes":
                test_overwrite_guard(quote_number, lines, payload)
        else:
            conn.rollback()
            print("Rolled back — nothing was written.")

    except Exception as exc:
        conn.rollback()
        print(f"\nERROR — rolled back, nothing was written.\n{exc}")
        raise
    finally:
        conn.close()

def test_overwrite_guard(quote_number: str, lines: list[QuoteLine], payload: dict) -> None:
    """
    Attempts a second write_quote() call against the same quote_number
    that was just committed. Expects a ValueError from the new
    pre-write existence check — confirms the guard actually blocks a
    duplicate rather than silently succeeding. Always rolls back
    regardless of outcome, since this is a negative test — nothing from
    this attempt should ever be committed.
    """
    conn = get_connection()
    conn.autocommit = False
    cursor = conn.cursor()

    print(f"\n=== Testing overwrite guard: second write to '{quote_number}' ===")
    try:
        write_quote(
            cursor,
            quote_number=quote_number,
            part_number=quote_number,
            description=f"DUPLICATE TEST — should never commit",
            lines=lines,
        )
        print("  FAIL — second write succeeded. Guard did not trigger.")
    except ValueError as exc:
        print(f"  PASS — guard raised ValueError as expected:\n    {exc}")
    except Exception as exc:
        print(f"  FAIL — unexpected exception type ({type(exc).__name__}):\n    {exc}")
    finally:
        conn.rollback()
        cursor.execute("SELECT Quote FROM Quote WHERE RFQ = ?", quote_number)
        count = len(cursor.fetchall())
        print(f"  Post-test check: {count} Quote row(s) under this RFQ (should be 1, from the first commit).")
        conn.close()

if __name__ == "__main__":
    main()