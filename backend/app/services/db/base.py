"""Connector protocol and shared helpers.

All dialect-specific connectors implement `BaseConnector` so the rest of
the codebase can stay dialect-agnostic. Connectors are used as
short-lived context managers:

    with get_connector(kb) as conn:
        tables = conn.list_tables()
        rows = conn.execute("SELECT ...")

`execute()` is synchronous because the underlying DB drivers are sync;
callers in async contexts must wrap with `asyncio.to_thread()`.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# Identifiers we never allow in a qualified column reference — defense in
# depth against accidental injection from prompt-influenced table names.
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str, dialect: str) -> str:
    """Quote a SQL identifier for the given dialect.

    Validates that `name` is a plain identifier (alphanumeric + underscore,
    not starting with a digit) before wrapping it — protects against
    prompt-injection attacks that try to smuggle SQL through table/column
    names. Raises ValueError if the name is not a safe identifier.
    """
    if not _SAFE_IDENT.match(name):
        raise ValueError(f"Unsafe identifier: {name!r}")
    if dialect == "mysql":
        return f"`{name}`"
    if dialect in ("postgres", "mssql"):
        return f"[{name}]" if dialect == "mssql" else f'"{name}"'
    if dialect == "oracle":
        return f'"{name}"'
    if dialect == "sqlite":
        return f'"{name}"'
    return f'"{name}"'


class BaseConnector(Protocol):
    """Protocol every dialect connector implements.

    Connectors are typically used as context managers; `close()` is
    called automatically on exit.
    """

    kb: Any  # the KnowledgeBase model (kept for source_name etc.)
    dialect: str

    def __enter__(self) -> "BaseConnector": ...
    def __exit__(self, exc_type, exc, tb) -> None: ...
    def close(self) -> None: ...

    def list_tables(self) -> list[str]:
        """Return table names visible in the current database/schema."""
        ...

    def describe_table(self, table: str) -> list[dict]:
        """Return column metadata for a table.

        Each dict has keys: name, type, nullable, default, pk (bool).
        """
        ...

    def execute(
        self,
        sql: str,
        params: dict | None = None,
        max_rows: int = 1000,
        timeout_s: int = 10,
    ) -> list[dict]:
        """Run a read SQL statement and return rows as list of dicts.

        Implementations must:
          - cap the result at `max_rows` rows
          - apply a `timeout_s` statement timeout where possible
          - raise on any error
        """
        ...

    def test_connection(self) -> dict:
        """Lightweight connectivity check. Returns {"ok": bool, "info": str}."""
        ...


def row_to_dict(columns: list[str], row: tuple) -> dict:
    """Convert a DBAPI row tuple + column names to a plain dict."""
    return {col: _safe_jsonify(row[i]) for i, col in enumerate(columns)}


def _safe_jsonify(value: Any) -> Any:
    """Convert DB return values into JSON-serializable primitives.

    Handles datetime, date, time, decimal, bytes, and UUID — all common
    return types from DB drivers.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if hasattr(value, "isoformat"):  # datetime/date/time
        return value.isoformat()
    if hasattr(value, "__float__"):  # Decimal
        return float(value)
    return str(value)
