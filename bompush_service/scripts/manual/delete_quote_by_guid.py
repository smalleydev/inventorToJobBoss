"""
Delete one test quote's rows (Quote_Req_Qty, Quote_Req, Quote_Qty,
Quote) by Quote GUID. TESTPROD only; see _testprod_guard.py.

RFQ and Integration staging rows are left alone.

Usage:
    python delete_quote_by_guid.py {a58457bd-31a2-4ebd-9bc4-50872e1abf08}
"""

import sys

from _testprod_guard import testprod_connection

TABLES_IN_DELETE_ORDER = ("Quote_Req_Qty", "Quote_Req", "Quote_Qty", "Quote")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python delete_quote_by_guid.py <QuoteGUID>")
    quote_guid = sys.argv[1]

    conn = testprod_connection()
    cursor = conn.cursor()

    answer = input(f"Delete all rows for Quote {quote_guid} from TESTPROD? Type 'yes': ")
    if answer.strip().lower() != "yes":
        print("Aborted, nothing deleted.")
        conn.close()
        return

    for table in TABLES_IN_DELETE_ORDER:
        cursor.execute(f"DELETE FROM {table} WHERE Quote = ?", quote_guid)
        print(f"{table}: {cursor.rowcount} deleted")

    conn.commit()
    print("Committed.")
    conn.close()


if __name__ == "__main__":
    main()
