"""
Edge case test #10: how does write_quote() actually scale on a large
BOM? Every test so far has been a 6-line assembly — this builds a
synthetic quote with many lines (reusing a handful of known-good
materials from earlier testing, since row COUNT is what we're timing,
not material variety) and measures real elapsed time.

Same safety pattern as test_write_quote.py: prints a plan, asks for
confirmation before writing, asks again before committing, rolls back
cleanly if you say no at either point.

Usage:
    python large_bom_test.py [line_count]
    (defaults to 300 lines if not given)
"""

import sys
import time
import uuid

from db import get_connection
from quote_writer import QuoteLine, write_quote

# Materials confirmed to exist in TESTPROD from earlier testing — mixed
# hardware (no length) and raw-stock (with a length) lines, so this
# exercises both code paths, not just one.
KNOWN_GOOD_MATERIALS = [
    ("11-0087", None),       # hardware, "each"
    ("11-0085", None),       # hardware, "each"
    ("06-0062", 24.0),       # round bar, single-length line
    ("06-0124", 50.0),       # tube, single-length line
]


def build_synthetic_lines(count: int) -> list[QuoteLine]:
    lines = []
    for i in range(count):
        material, length = KNOWN_GOOD_MATERIALS[i % len(KNOWN_GOOD_MATERIALS)]
        lines.append(QuoteLine(
            jobboss_material=material,
            quantity=1,
            cut_length_in=length,
        ))
    return lines


def main():
    line_count = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    quote_number = f"SCALE_TEST_{uuid.uuid4().hex[:8].upper()}"

    lines = build_synthetic_lines(line_count)

    print(f"Quote number: {quote_number}")
    print(f"Line count:   {len(lines)}")
    print("(reusing 4 known-good materials repeatedly — testing row count, not variety)\n")

    confirm = input("Write this to TESTPROD as a real quote? (yes/no): ")
    if confirm.strip().lower() != "yes":
        print("Aborted, nothing written.")
        return

    conn = get_connection()
    conn.autocommit = False
    cursor = conn.cursor()

    try:
        start = time.perf_counter()

        quote_guid = write_quote(
            cursor,
            quote_number=quote_number,
            part_number=quote_number,
            description="Scale test — synthetic large BOM",
            lines=lines,
        )

        elapsed = time.perf_counter() - start

        print(f"\nWrote quote. GUID: {quote_guid}")
        print(f"Elapsed (before commit): {elapsed:.2f}s total, "
              f"{elapsed / len(lines) * 1000:.1f}ms per line")

        final_confirm = input("\nCommit this write? (yes/no): ")

        if final_confirm.strip().lower() == "yes":
            commit_start = time.perf_counter()
            conn.commit()
            commit_elapsed = time.perf_counter() - commit_start
            print(f"Committed. Commit itself took {commit_elapsed:.2f}s.")
            print(f"\nTotal end-to-end: {elapsed + commit_elapsed:.2f}s for {len(lines)} lines.")
        else:
            conn.rollback()
            print("Rolled back — nothing was written.")

    except Exception as exc:
        conn.rollback()
        print(f"\nERROR at {time.perf_counter() - start:.2f}s in — rolled back, nothing written.\n{exc}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()