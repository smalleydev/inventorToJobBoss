"""
Shared database connection helper for JL Check and the push service.

Every script that touches JobBOSS's SQL Server gets its connection through
get_connection() rather than building a connection string inline. This
guarantees the environment is verified before any query runs, and gives
one place to change connection details later (moving from TESTPROD to
PRODUCTION, or from a workstation to the server).
"""

import sys

import pyodbc

# Connection details for the JobBOSS test environment. Windows Integrated
# Authentication — no credentials stored here.
CONN_STR = (
    "Driver={ODBC Driver 18 for SQL Server};"
    "Server=JBSERVER\\SQLEXPRESS;"
    "Database=PRODUCTION;"
    "Trusted_Connection=Yes;"
    "Encrypt=Yes;"
    "TrustServerCertificate=Yes;"
)

# The only database this codebase is currently allowed to touch.
# Intentionally hardcoded rather than parsed out of CONN_STR at runtime —
# this is a deliberate safety check, so a typo'd or copy-pasted connection
# string pointing at PRODUCTION gets caught immediately instead of
# silently accepted.
EXPECTED_DATABASE = "PRODUCTION"


def get_connection() -> pyodbc.Connection:
    """
    Open a connection and verify it is pointed at EXPECTED_DATABASE before
    returning it.

    Raises RuntimeError (and refuses to hand back a live connection) if
    the database doesn't match — the single choke point that prevents any
    script from accidentally running against the wrong environment.
    """
    conn = pyodbc.connect(CONN_STR)

    cursor = conn.cursor()
    cursor.execute("SELECT DB_NAME()")
    actual_database = cursor.fetchone()[0]

    if actual_database != EXPECTED_DATABASE:
        conn.close()
        raise RuntimeError(
            f"Refusing to proceed: connected to '{actual_database}', "
            f"expected '{EXPECTED_DATABASE}'. Check the connection string "
            f"in db.py before running this script again."
        )

    return conn


if __name__ == "__main__":
    # Running this file directly performs the verification and exits —
    # useful as a standalone sanity check of connectivity + environment.
    try:
        connection = get_connection()
        print(f"Connected and verified: {EXPECTED_DATABASE}")
        connection.close()
    except (RuntimeError, pyodbc.Error) as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)
