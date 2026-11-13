"""
TESTPROD-only connection for the manual scripts in this folder.

These scripts write to (or read from) JobBOSS tables directly. db.py on
main points at PRODUCTION, so calling db.get_connection() from here
would reach the live database. testprod_connection() refuses to even
open a connection unless db.py is configured for TESTPROD, then checks
DB_NAME() again once connected.

Temporary: replaced by the shared jbcore config in a later phase.
"""

import re
import sys
from pathlib import Path

# Make the JL Check modules (db, ...) importable
# from this subfolder.
APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import db  # noqa: E402

REQUIRED_DATABASE = "TESTPROD"


def _configured_database() -> str | None:
    match = re.search(r"Database=([^;]+);", db.CONN_STR, re.IGNORECASE)
    return match.group(1).strip() if match else None


def testprod_connection():
    """Open a connection only if db.py targets TESTPROD; exit otherwise.

    The check happens BEFORE connecting, so a PRODUCTION-configured
    db.py never opens a connection at all.
    """
    configured = _configured_database()
    if configured != REQUIRED_DATABASE or db.EXPECTED_DATABASE != REQUIRED_DATABASE:
        sys.exit(
            f"Refusing to run: db.py is configured for '{configured}' "
            f"(EXPECTED_DATABASE='{db.EXPECTED_DATABASE}'). Manual scripts "
            f"run against {REQUIRED_DATABASE} only."
        )

    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DB_NAME()")
    actual = cursor.fetchone()[0]
    if actual != REQUIRED_DATABASE:
        conn.close()
        sys.exit(f"Refusing to run: connected to '{actual}', not {REQUIRED_DATABASE}.")
    return conn
