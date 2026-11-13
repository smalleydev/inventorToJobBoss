"""
Shared test setup.

SAFETY: unit tests must never reach a real database. db.py in both apps
points at PRODUCTION on main, so this file disables pyodbc.connect
before any app module is imported. Anything that tries to connect fails
immediately with DatabaseAccessBlocked instead of opening a connection.
"""

import sys
import types

import pytest


class DatabaseAccessBlocked(RuntimeError):
    """Raised when code under test tries to open a real DB connection."""


def _blocked_connect(*_args, **_kwargs):
    raise DatabaseAccessBlocked(
        "Unit tests must not open database connections. Use tests.fakes.FakeCursor."
    )


try:
    import pyodbc
except ImportError:
    # Lets the pure-logic tests run on machines (CI) without the ODBC
    # driver stack. Only the names the app code references are provided.
    pyodbc = types.ModuleType("pyodbc")

    class Error(Exception):
        pass

    class DatabaseError(Error):
        pass

    class IntegrityError(DatabaseError):
        pass

    pyodbc.Error = Error
    pyodbc.DatabaseError = DatabaseError
    pyodbc.IntegrityError = IntegrityError
    pyodbc.Connection = object
    sys.modules["pyodbc"] = pyodbc

pyodbc.connect = _blocked_connect


@pytest.fixture(autouse=True)
def _keep_database_blocked(monkeypatch):
    """Re-assert the block per test, in case a test or module rebinds it."""
    monkeypatch.setattr(pyodbc, "connect", _blocked_connect)
