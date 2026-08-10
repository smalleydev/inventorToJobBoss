"""
Edge case test #2: the actual race try_claim_quote's PRIMARY KEY lock
is meant to prevent — two separate processes (or threads with separate
connections, standing in for two watcher_service.py instances) racing
to claim the SAME quote number at the same moment.

Uses a threading.Barrier to hold both threads at the starting line
until both are ready, then release them together — this gets them as
close to a genuine simultaneous INSERT as we can force from a single
machine, far tighter than trying to time two manual file-drops by hand.

Expected result: exactly one thread gets True, the other gets False,
and exactly one header row exists afterward. If both ever got True, or
if two header rows existed, the lock would be broken.

Run: python race_test.py
"""

import threading
import time
import uuid

from db import get_connection
from staging import try_claim_quote

# Fresh quote number every run, so repeated test runs never collide
# with each other's leftover data.
TEST_QUOTE = f"RACE_TEST_{uuid.uuid4().hex[:8].upper()}"

results = {}
barrier = threading.Barrier(2)


def attempt_claim(thread_name: str):
    conn = get_connection()
    conn.autocommit = False
    cursor = conn.cursor()

    # Wait here until both threads are ready, then everyone proceeds
    # together — this is what makes the race actually tight.
    barrier.wait()

    try:
        claimed = try_claim_quote(cursor, TEST_QUOTE, source_file=f"race_test_{thread_name}")
        results[thread_name] = claimed
    finally:
        conn.close()


def main():
    print(f"Testing quote number: {TEST_QUOTE}")
    print("Firing two simultaneous claim attempts...\n")

    t1 = threading.Thread(target=attempt_claim, args=("A",))
    t2 = threading.Thread(target=attempt_claim, args=("B",))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    print(f"Thread A claimed: {results.get('A')}")
    print(f"Thread B claimed: {results.get('B')}")

    successes = sum(1 for v in results.values() if v is True)

    print(f"\n{'PASS' if successes == 1 else 'FAIL'}: {successes} thread(s) succeeded (expected exactly 1)")

    # Verify only one header row actually exists — the real proof, not
    # just trusting the return values.
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) AS c FROM Integration.BOM_Staging_Header WHERE QuoteNumber = ?",
        TEST_QUOTE,
    )
    row_count = cursor.fetchone().c
    print(f"{'PASS' if row_count == 1 else 'FAIL'}: {row_count} header row(s) exist (expected exactly 1)")
    conn.close()


if __name__ == "__main__":
    main()