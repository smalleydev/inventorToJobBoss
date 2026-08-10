"""
Run this AFTER killing mid_write_kill_test.py via Task Manager, to
verify the abandoned transaction really did roll back — i.e. the
RFQ/Quote rows it printed as "inserted" never actually persisted.

Usage:
    python mid_write_kill_test_verify.py KILL_TEST_XXXXXXXX
"""

import sys

from db import get_connection


def main():
    if len(sys.argv) < 2:
        print("Usage: python mid_write_kill_test_verify.py KILL_TEST_XXXXXXXX")
        sys.exit(1)

    test_quote = sys.argv[1]
    print(f"Verifying: {test_quote}\n")

    conn = get_connection()
    cursor = conn.cursor()

    print("=== Integration.BOM_Staging_Header ===")
    cursor.execute(
        "SELECT * FROM Integration.BOM_Staging_Header WHERE QuoteNumber = ?",
        test_quote,
    )
    columns = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    if row:
        print(" ", dict(zip(columns, row)))
        expected_pending = row.Status == "PENDING"
        print(f"\n{'PASS' if expected_pending else 'FAIL'}: "
              f"Status is {row.Status} (expected PENDING — a genuine "
              f"stuck claim from the killed process)")
    else:
        print("  NOT FOUND — unexpected, the claim should have committed.")

    print("\n=== dbo.RFQ ===")
    cursor.execute("SELECT COUNT(*) AS c FROM RFQ WHERE RFQ = ?", test_quote)
    rfq_count = cursor.fetchone().c
    print(f"  {rfq_count} row(s)")
    print(f"{'PASS' if rfq_count == 0 else 'FAIL'}: "
          f"Expected 0 — the RFQ insert should have rolled back with "
          f"the rest of the abandoned transaction.")

    print("\n=== dbo.Quote (by Description, since we don't have the GUID here) ===")
    cursor.execute("SELECT COUNT(*) AS c FROM Quote WHERE RFQ = ?", test_quote)
    quote_count = cursor.fetchone().c
    print(f"  {quote_count} row(s)")
    print(f"{'PASS' if quote_count == 0 else 'FAIL'}: "
          f"Expected 0 — same reasoning, this should never have "
          f"persisted without a commit.")

    print(
        "\nIf all three show PASS, sir: the stuck PENDING row is exactly "
        "as safe as we've been claiming — no partial JobBOSS data exists, "
        "and the manual reset process (UPDATE Status='ERROR') is genuinely "
        "sufficient to unstick it."
    )

    conn.close()


if __name__ == "__main__":
    main()