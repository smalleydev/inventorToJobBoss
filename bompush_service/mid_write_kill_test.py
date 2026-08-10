"""
Edge case test #3: what actually happens if watcher_service.py is
killed mid-write (not a clean Ctrl+C, a hard kill via Task Manager),
after some real INSERT statements have run but before the transaction
commits.

We've asserted all along that this is safe — an abandoned, uncommitted
transaction rolls back automatically, so a stuck PENDING staging row
never corresponds to any real partial data in JobBOSS. This script
tests that claim directly instead of continuing to assume it.

Deliberately does NOT call write_quote() from quote_writer.py — this is
a standalone script that duplicates just its first two steps (RFQ +
Quote insert) so the real, unmodified quote_writer.py never needs to be
touched or temporarily hacked for this test.

HOW TO RUN THIS TEST:
  1. Run: python mid_write_kill_test.py
  2. It claims a fresh test quote and writes RFQ + Quote rows in an
     open transaction, then counts down.
  3. During the countdown, open Task Manager and forcibly end this
     python.exe process (End Task) — do NOT press Ctrl+C, that's a
     clean shutdown and won't test the same thing.
  4. Run mid_write_kill_test_verify.py with the TEST_QUOTE this script
     printed, to confirm nothing was left behind.
"""

import time
import uuid

from db import get_connection
from staging import try_claim_quote

TEST_QUOTE = f"KILL_TEST_{uuid.uuid4().hex[:8].upper()}"
COUNTDOWN_SECONDS = 15


def main():
    print(f"Testing quote number: {TEST_QUOTE}")
    print("(save this — you'll need it for the verification script)\n")

    conn = get_connection()
    conn.autocommit = False
    cursor = conn.cursor()

    claimed = try_claim_quote(cursor, TEST_QUOTE, source_file="mid_write_kill_test")
    print(f"Claimed: {claimed}  (staging header committed as PENDING — this part is real)")

    if not claimed:
        print("Could not claim — try again, this generates a fresh quote number each run.")
        return

    cursor.execute("SET NOCOUNT ON")

    # Same RFQ insert quote_writer.write_quote uses — duplicated here on
    # purpose rather than calling the real function, so that file stays
    # completely untouched for this test.
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
        TEST_QUOTE, "TEST",
    )
    print("Inserted RFQ row (UNCOMMITTED).")

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
        quote_guid, quote_guid, "Mid-write kill test", TEST_QUOTE, "TEST",
    )
    print(f"Inserted Quote row (UNCOMMITTED). GUID would be {quote_guid}.")

    print(
        f"\n>>> Transaction is OPEN, nothing committed yet. <<<\n"
        f">>> Open Task Manager NOW and End Task this python.exe "
        f"process — do not Ctrl+C. <<<\n"
    )

    for remaining in range(COUNTDOWN_SECONDS, 0, -1):
        print(f"  Killing in {remaining}s... (still here = you didn't kill it yet)")
        time.sleep(1)

    # If we get here, the process was NOT killed — commit normally so
    # this doesn't leave a dangling claim, and note the test wasn't
    # actually performed.
    conn.commit()
    print(
        "\nNot killed in time — committed normally instead. "
        "Re-run and actually End Task this time to perform the real test."
    )
    conn.close()


if __name__ == "__main__":
    main()