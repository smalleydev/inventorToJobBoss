"""Proves the safety nets hold: no unit test can open a DB connection, and
the manual scripts refuse anything but TESTPROD before connecting."""

import importlib.util
from pathlib import Path

import pyodbc
import pytest

import db
from tests.conftest import DatabaseAccessBlocked

REPO = Path(__file__).resolve().parents[2]
GUARDS = [
    REPO / "bompush_service" / "scripts" / "manual" / "_testprod_guard.py",
    REPO / "jl_check" / "scripts" / "_testprod_guard.py",
]


def test_pyodbc_connect_is_blocked():
    with pytest.raises(DatabaseAccessBlocked):
        pyodbc.connect("Driver=x;Database=TESTPROD;")


def test_get_connection_is_blocked():
    with pytest.raises(DatabaseAccessBlocked):
        db.get_connection()


def load_guard(path):
    spec = importlib.util.spec_from_file_location(f"guard_{path.parent.name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", GUARDS, ids=lambda p: p.parts[-3])
def test_guard_refuses_production_without_connecting(path, monkeypatch):
    guard = load_guard(path)
    connects = []
    monkeypatch.setattr(guard.db, "get_connection", lambda: connects.append(1))
    monkeypatch.setattr(guard.db, "CONN_STR", "Driver=x;Database=PRODUCTION;")
    monkeypatch.setattr(guard.db, "EXPECTED_DATABASE", "PRODUCTION")
    with pytest.raises(SystemExit, match="Refusing to run"):
        guard.testprod_connection()
    assert connects == []


@pytest.mark.parametrize("path", GUARDS, ids=lambda p: p.parts[-3])
def test_guard_needs_both_settings_on_testprod(path, monkeypatch):
    guard = load_guard(path)
    monkeypatch.setattr(guard.db, "CONN_STR", "Driver=x;Database=TESTPROD;")
    monkeypatch.setattr(guard.db, "EXPECTED_DATABASE", "PRODUCTION")
    with pytest.raises(SystemExit, match="Refusing to run"):
        guard.testprod_connection()


@pytest.mark.parametrize("path", GUARDS, ids=lambda p: p.parts[-3])
def test_guard_rechecks_db_name_after_connecting(path, monkeypatch):
    from tests.fakes import FakeCursor

    guard = load_guard(path)
    cursor = FakeCursor().on("SELECT DB_NAME()", [("PRODUCTION",)])
    monkeypatch.setattr(guard.db, "CONN_STR", "Driver=x;Database=TESTPROD;")
    monkeypatch.setattr(guard.db, "EXPECTED_DATABASE", "TESTPROD")
    monkeypatch.setattr(guard.db, "get_connection", lambda: cursor.connection)
    with pytest.raises(SystemExit, match="connected to 'PRODUCTION'"):
        guard.testprod_connection()
    assert cursor.connection.closed
