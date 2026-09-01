"""MySQL datasource adapter — wraps ``MySQLConnector`` (SQLAlchemy + pymysql).

Implements the ``DatasourceAdapter`` Protocol so that the NL2SQL pipeline
(and ``render_m_schema``) can work with MySQL KnowledgeBases the same way
they work with SQLite / Postgres.

Type normalisation
------------------
MySQL returns lowercase type names like ``varchar(255)``, ``int``,
``decimal(10,2)``, ``longtext``.  ``render_m_schema`` checks
``col.dtype.upper() in ("TEXT", "VARCHAR", "CHARACTER VARYING")`` to decide
whether to sample distinct values.  This adapter normalises the base type
so that:

* ``longtext / mediumtext / tinytext / text`` → ``TEXT``
* ``char(N)`` → ``VARCHAR``
* ``varchar(N)`` → ``VARCHAR``
* ``enum / set`` → ``TEXT``  (sampled like text columns)
* other types keep their uppercased base name (``INT``, ``DECIMAL``, …)
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from app.services.datasources import (
    ColumnInfo,
    DatasourceAdapter,
    ExplainResult,
    QueryResult,
)
from app.services.db.mysql import MySQLConnector

logger = logging.getLogger(__name__)

# ── type normalisation ──────────────────────────────────────────────────

# Map MySQL base types → M-Schema convention (uppercased, stripped of length).
_TEXT_TYPES = frozenset({"text", "longtext", "mediumtext", "tinytext"})
_VARCHAR_EQUIV = frozenset({"varchar", "char"})
_TEXT_EQUIV = frozenset({"enum", "set"})


def _normalise_mysql_type(raw: str) -> str:
    """Normalise a MySQL column type to M-Schema convention.

    Examples:
        ``varchar(255)`` → ``VARCHAR``
        ``longtext``      → ``TEXT``
        ``int``           → ``INT``
        ``decimal(10,2)`` → ``DECIMAL``
    """
    # Strip length/precision: "varchar(255)" → "varchar", "decimal(10,2)" → "decimal"
    base = re.sub(r"\(.*\)", "", raw).strip().lower()
    if base in _TEXT_TYPES:
        return "TEXT"
    if base in _VARCHAR_EQUIV:
        return "VARCHAR"
    if base in _TEXT_EQUIV:
        return "TEXT"
    return base.upper()


class MySQLAdapter:
    """MySQL adapter satisfying the ``DatasourceAdapter`` Protocol.

    Wraps the existing ``MySQLConnector`` (reuses SQLAlchemy + pymysql)
    rather than re-implementing connection logic.
    """

    quote_char = '`'

    def __init__(
        self,
        kb: Any,
        *,
        timeout_ms: int = 5000,
    ):
        self._kb = kb
        self._timeout_ms = timeout_ms
        self._connector: MySQLConnector | None = None

    # ── helpers ───────────────────────────────────────────────────────

    def _get_connector(self) -> MySQLConnector:
        """Lazily create and return a ``MySQLConnector`` context."""
        if self._connector is None:
            self._connector = MySQLConnector(self._kb)
            self._connector.__enter__()
        return self._connector

    # ── Protocol methods ──────────────────────────────────────────────

    def test_connection(self) -> bool:
        try:
            conn = self._get_connector()
            # Use the existing engine directly instead of MySQLConnector.test_connection()
            # which creates a separate engine via `with self:`.
            from sqlalchemy import text as sa_text
            with conn._engine.connect() as c:
                c.execute(sa_text("SELECT 1"))
            return True
        except Exception as e:
            logger.warning("MySQL connection test failed: %s", e)
            return False

    def list_tables(self) -> list[str]:
        return self._get_connector().list_tables()

    def describe_table(self, table: str) -> list[ColumnInfo]:
        raw_cols = self._get_connector().describe_table(table)
        result: list[ColumnInfo] = []
        for c in raw_cols:
            raw_type = str(c.get("type", ""))
            normalised = _normalise_mysql_type(raw_type)
            result.append(ColumnInfo(
                name=str(c.get("name", "")),
                dtype=normalised,
                nullable=bool(c.get("nullable", True)),
                default=str(c["default"]) if c.get("default") is not None else None,
                is_pk=bool(c.get("pk", False)),
                extra={"raw_type": raw_type} if raw_type != normalised else {},
            ))
        return result

    def refresh_schema(self) -> dict[str, list[ColumnInfo]]:
        tables = self.list_tables()
        return {t: self.describe_table(t) for t in tables}

    def explain(self, sql: str, params: dict[str, Any] | None = None) -> ExplainResult:
        """Best-effort EXPLAIN — returns empty result on failure."""
        try:
            conn = self._get_connector()
            explain_sql = f"EXPLAIN FORMAT=JSON {sql}"
            rows = conn.execute(explain_sql, params or {}, max_rows=1, timeout_s=5)
            if rows:
                import json as _json
                plan = _json.loads(rows[0].get("EXPLAIN", "{}")) if isinstance(rows[0], dict) else {}
                plan_json = [plan] if isinstance(plan, dict) else plan if isinstance(plan, list) else []
                return ExplainResult(
                    plan_json=plan_json,
                    plan_text=str(plan_json),
                    estimated_cost=0.0,
                    estimated_rows=0,
                )
        except Exception as e:
            logger.debug("MySQL EXPLAIN failed (non-fatal): %s", e)
        return ExplainResult(plan_json=[], plan_text="", estimated_cost=0.0, estimated_rows=0)

    def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        row_limit: int = 1000,
        timeout_ms: int = 5000,
    ) -> QueryResult:
        conn = self._get_connector()
        t0 = time.monotonic()
        rows = conn.execute(
            sql,
            params or {},
            max_rows=row_limit,
            timeout_s=max(1, timeout_ms // 1000),
        )
        elapsed = (time.monotonic() - t0) * 1000

        if not rows:
            return QueryResult(columns=[], rows=[], row_count=0, duration_ms=round(elapsed, 2))

        columns = list(rows[0].keys()) if isinstance(rows[0], dict) else []
        tuples = [tuple(r.values()) for r in rows] if isinstance(rows[0], dict) else [tuple(r) for r in rows]
        return QueryResult(
            columns=columns,
            rows=tuples,
            row_count=len(tuples),
            duration_ms=round(elapsed, 2),
        )

    def close(self) -> None:
        if self._connector is not None:
            try:
                self._connector.__exit__(None, None, None)
            except Exception as e:
                logger.debug("MySQL adapter close failed: %s", e)
            self._connector = None
