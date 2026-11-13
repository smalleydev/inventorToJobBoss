"""
In-memory stand-ins for pyodbc cursors and connections.

FakeCursor answers each execute() by asking its handlers, in order, for
rows. A handler is ``handler(sql, params) -> list | None``; None means
"not mine, ask the next one". Unhandled statements (INSERTs, SET
NOCOUNT ON) return no rows. Every call is recorded in ``cursor.calls``
so tests can assert on the exact statement sequence.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

Handler = Callable[[str, tuple], "list | None"]


class FakeRow:
    """Row that supports pyodbc's attribute access AND index access."""

    def __init__(self, values: Mapping[str, Any] | Iterable[Any]):
        if isinstance(values, Mapping):
            self._names = list(values.keys())
            self._values = list(values.values())
        else:
            self._values = list(values)
            self._names = [f"col{i}" for i in range(len(self._values))]

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[self._names.index(name)]
        except ValueError:
            raise AttributeError(name) from None

    def __getitem__(self, index: int) -> Any:
        return self._values[index]

    def __repr__(self) -> str:
        return f"FakeRow({dict(zip(self._names, self._values, strict=True))})"


@dataclass
class Call:
    sql: str  # whitespace-collapsed
    params: tuple


def normalize_sql(sql: str) -> str:
    return " ".join(sql.split())


class FakeConnection:
    def __init__(self, cursor: FakeCursor | None = None):
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._cursor = cursor

    def cursor(self) -> FakeCursor:
        if self._cursor is None:
            self._cursor = FakeCursor(connection=self)
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeCursor:
    def __init__(self, handlers: Iterable[Handler] = (), connection: FakeConnection | None = None):
        self.handlers: list[Handler] = list(handlers)
        self.calls: list[Call] = []
        self.rowcount = -1
        self.connection = connection or FakeConnection(self)
        self._pending: list[FakeRow] = []

    def on(self, fragment: str, rows: list | Callable[[tuple], list]) -> FakeCursor:
        """Answer statements containing ``fragment`` with ``rows`` (or rows(params))."""
        needle = normalize_sql(fragment)

        def handler(sql: str, params: tuple):
            if needle not in sql:
                return None
            return rows(params) if callable(rows) else rows

        self.handlers.append(handler)
        return self

    def execute(self, sql: str, *params: Any) -> FakeCursor:
        norm = normalize_sql(sql)
        self.calls.append(Call(norm, tuple(params)))
        result: list = []
        for handler in self.handlers:
            answer = handler(norm, tuple(params))
            if answer is not None:
                result = answer
                break
        self._pending = [r if isinstance(r, FakeRow) else FakeRow(r) for r in result]
        self.rowcount = len(self._pending)
        return self

    def fetchone(self) -> FakeRow | None:
        return self._pending.pop(0) if self._pending else None

    def fetchall(self) -> list[FakeRow]:
        rows, self._pending = self._pending, []
        return rows

    # Convenience for assertions -------------------------------------------
    def statements(self) -> list[str]:
        return [c.sql for c in self.calls]

    def calls_matching(self, fragment: str) -> list[Call]:
        needle = normalize_sql(fragment)
        return [c for c in self.calls if needle in c.sql]


# --- JobBOSS Material table -------------------------------------------------

MATERIAL_DEFAULTS = {
    "Description": "",
    "Ext_Description": "",
    "Type": "H",
    "Pick_Buy_Indicator": "P",
    "Primary_Vendor": None,
    "Stocked_UofM": "ea",
    "Cost_UofM": "ea",
    "Standard_Cost": 0.0,
    "IS_Length": None,
}


def _unescape_like(pattern: str) -> str:
    """Inverse of jobboss_lookup._escape_like, minus the trailing '%'."""
    assert pattern.endswith("%"), pattern
    body = pattern[:-1]
    return body.replace("[[]", "[").replace("[%]", "%").replace("[_]", "_")


def material_table(materials: Mapping[str, Mapping[str, Any]]) -> list[Handler]:
    """Handlers that answer the app's Material queries from a dict.

    ``materials`` maps Material number -> column overrides. Covers the
    exact-match SELECTs (any column list) and the prefix LIKE search.
    """
    table = {k: {**MATERIAL_DEFAULTS, "Material": k, **v} for k, v in materials.items()}

    def like_search(sql: str, params: tuple):
        if "FROM Material WHERE Material LIKE ?" not in sql:
            return None
        prefix = _unescape_like(params[0])
        limit = int(re.search(r"TOP (\d+)", sql).group(1)) if "TOP" in sql else None
        hits = [table[k] for k in sorted(table) if k.startswith(prefix)]
        return hits[:limit] if limit else hits

    def exact(sql: str, params: tuple):
        if "FROM Material WHERE Material = ?" not in sql:
            return None
        row = table.get(params[0])
        return [row] if row else []

    return [like_search, exact]
