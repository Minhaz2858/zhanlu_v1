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

    Dialect rules live in ``services/db/dialect.py`` (one table, five dialects)
    rather than in this if/elif chain, which had no entry for MariaDB, SQL
    Server brackets or Oracle and silently emitted double quotes for anything
    it did not recognise.

    An unrecognised dialect (including the legacy ``"default"`` sentinel used by
    formatting-only callers) falls back to ANSI double quotes and logs at debug
    level — this function is for *formatting* identifiers. EXECUTION paths must
    use a DialectProfile (``services/db/dialect.profile_for``), which raises
    ``UnknownDialect`` instead of guessing.
    """
    from app.services.db.dialect import UnknownDialect, ansi_quote_ident, profile_for

    if not _SAFE_IDENT.match(name):
        raise ValueError(f"Unsafe identifier: {name!r}")
    try:
        return profile_for(dialect).quote_ident(name)
    except UnknownDialect:
        logger.debug(
            "quote_ident: no capability profile for dialect %r — using ANSI quoting", dialect
        )
        return ansi_quote_ident(name)


class BaseConnector(Protocol):
    """Protocol every dialect connector implements.

    Connectors are typically used as context managers; `close()` is
    called automatically on exit.
    """

    kb: Any  # the KnowledgeBase model (kept for source_name etc.)
    dialect: str
    #: The statement the driver ACTUALLY received — the authored SQL after any
    #: dialect rewrite (the MySQL ``/*+ MAX_EXECUTION_TIME */`` prefix, Oracle's
    #: ``ROWNUM`` wrapper). Dialects differ here, so an audit trail that keeps
    #: only the authored text cannot show what was executed. Set by ``execute()``.
    last_executed_sql: str

    def __enter__(self) -> "BaseConnector": ...
    def __exit__(self, exc_type, exc, tb) -> None: ...
    def close(self) -> None: ...

    def list_tables(self) -> list[str]:
        """Return table names visible in the current database/schema."""
        ...

    def describe_table(self, table: str) -> list[dict]:
        """Return column metadata for a table.

        Each dict has keys: name, type, nullable, default, pk (bool), and
        optionally ``comment`` (the source's own column comment, verbatim) and
        ``key`` (index role: 'PRI' | 'UNI' | 'MUL' | ''). A dialect that cannot
        supply a comment returns ``None`` — never a placeholder and never our
        own inference, because the catalog distinguishes "the source says
        nothing about this column" from "the source says this".
        """
        ...

    def shape(self) -> list[tuple[str, str]]:
        """Return every visible (table, column) name pair in ONE query.

        This is the cheap change-detector behind ``schema_shape``: it must not
        degrade into a per-table loop (``describe_table`` N times is ~25 s on a
        143-table warehouse, versus ~1 s for one information_schema scan).

        Order and case do not matter — ``schema_shape.normalize_pairs`` folds both.
        Implementations should include views when the dialect's own ``list_tables``
        does not filter them out, and must stay read-only.
        """
        ...

    def table_facts(self, table: str) -> dict:
        """Optional: the source's own facts about a TABLE (never its rows).

        Keys — omit what the dialect cannot answer, never invent a value:

            comment        the table's own comment, verbatim
            table_type     'BASE TABLE' | 'VIEW' | dialect equivalent
            engine         storage engine, where one exists
            est_rows       the ENGINE's row estimate (statistics, not a count)
            size_bytes     data (+ index) allocation, engine-reported

        Implemented by mysql / postgres / sqlite. A dialect that does not
        implement it returns ``{}``, which the catalog records as "not
        collected" instead of zero.
        """
        ...

    def get_indexes(self, table: str) -> list[dict]:
        """Optional: the source's index inventory for one table.

        ``[{"name": str, "columns": [str], "unique": bool, "primary": bool}]``.
        An empty list asserts the source declares no index on this object; a
        dialect that cannot answer raises instead, so callers can tell "no
        indexes" apart from "we could not ask".
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
