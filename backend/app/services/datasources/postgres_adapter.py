"""PostgreSQL datasource adapter — ``psycopg`` with ``EXPLAIN (ANALYZE, FORMAT JSON)``."""

from __future__ import annotations

import logging
import time
from typing import Any

import psycopg

from app.services.datasources import (
    ColumnInfo,
    DatasourceAdapter,
    ExplainResult,
    QueryResult,
)

logger = logging.getLogger(__name__)


class PostgresAdapter:
    """Postgres adapter satisfying the ``DatasourceAdapter`` Protocol."""

    quote_char = '"'

    def __init__(
        self,
        dsn: str = "",
        *,
        host: str = "localhost",
        port: int = 5432,
        dbname: str = "zhanlu",
        user: str = "zhanlu",
        password: str = "",
        schema: str = "public",
        timeout_ms: int = 5000,
    ):
        self._config: dict[str, Any] = {
            "host": host,
            "port": port,
            "dbname": dbname,
            "user": user,
            "password": password,
            "connect_timeout": max(1, int(timeout_ms / 1000)),
        }
        if dsn:
            self._config = {"dsn": dsn, **self._config}
        self._timeout_ms = timeout_ms
        self._schema = schema or "public"
        # Pin search_path for non-default schemas so unqualified table names
        # in value-sampling queries resolve to the right schema.
        if self._schema != "public":
            safe = self._schema.replace('"', '""')
            self._config["options"] = f'-csearch_path="{safe}"'
        self._conn: psycopg.Connection | None = None

    # ── helpers ───────────────────────────────────────────────────────

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            dsn = self._config.pop("dsn", None)
            if dsn:
                self._conn = psycopg.connect(dsn, **self._config)
            else:
                self._conn = psycopg.connect(**self._config)
            self._conn.execute("SET statement_timeout = %s", (str(self._timeout_ms),))
        return self._conn

    # ── Protocol methods ──────────────────────────────────────────────

    def test_connection(self) -> bool:
        try:
            self._get_conn().execute("SELECT 1")
            return True
        except Exception as e:
            logger.warning("Postgres connection test failed: %s", e)
            return False

    def list_tables(self) -> list[str]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name",
            (self._schema,),
        ).fetchall()
        return [r[0] for r in rows]

    def describe_table(self, table: str) -> list[ColumnInfo]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT
                c.column_name,
                c.data_type,
                c.is_nullable,
                c.column_default,
                (CASE WHEN pk.column_name IS NOT NULL THEN true ELSE false END) AS is_pk
            FROM information_schema.columns c
            LEFT JOIN (
                SELECT ku.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage ku
                  ON tc.constraint_name = ku.constraint_name
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = %s
                  AND tc.table_name = %s
            ) pk ON c.column_name = pk.column_name
            WHERE c.table_schema = %s
              AND c.table_name = %s
            ORDER BY c.ordinal_position
            """,
            (self._schema, table, self._schema, table),
        ).fetchall()
        result: list[ColumnInfo] = []
        for r in rows:
            result.append(ColumnInfo(
                name=r[0],
                dtype=str(r[1]),
                nullable=r[2] == "YES",
                default=str(r[3]) if r[3] is not None else None,
                is_pk=bool(r[4]),
            ))
        return result

    def refresh_schema(self) -> dict[str, list[ColumnInfo]]:
        tables = self.list_tables()
        return {t: self.describe_table(t) for t in tables}

    def explain(self, sql: str, params: dict[str, Any] | None = None) -> ExplainResult:
        conn = self._get_conn()
        explain_sql = f"EXPLAIN (ANALYZE false, FORMAT JSON) {sql}"
        rows = conn.execute(explain_sql, params or {}).fetchall()
        plan = rows[0][0] if rows else []
        if isinstance(plan, str):
            import json as _json
            plan = _json.loads(plan)
        plan_json: list[Any] = plan if isinstance(plan, list) else [plan]
        cost = self._extract_cost(plan_json)
        est_rows = self._extract_estimated_rows(plan_json)
        plan_text = str(plan_json)
        return ExplainResult(
            plan_json=plan_json,
            plan_text=plan_text,
            estimated_cost=cost,
            estimated_rows=est_rows,
        )

    def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        row_limit: int = 1000,
        timeout_ms: int = 5000,
    ) -> QueryResult:
        conn = self._get_conn()
        # Apply row limit at SQL level
        limited_sql = f"{sql.rstrip(';')} LIMIT {int(row_limit)}"
        t0 = time.monotonic()
        cur = conn.execute(limited_sql, params or {})
        rows = cur.fetchall()
        elapsed = (time.monotonic() - t0) * 1000
        cols = [d.name for d in cur.description] if cur.description else []
        return QueryResult(
            columns=cols,
            rows=[tuple(r) for r in rows],
            row_count=len(rows),
            duration_ms=round(elapsed, 2),
        )

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None

    # ── internal ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_cost(plan_json: list[Any]) -> float:
        total = 0.0
        for node in plan_json:
            if isinstance(node, dict):
                total += float(node.get("Plan", {}).get("Total Cost", 0))
                for child in node.get("Plan", {}).get("Plans", []):
                    total += float(child.get("Total Cost", 0))
        return total

    @staticmethod
    def _extract_estimated_rows(plan_json: list[Any]) -> int:
        total = 0
        for node in plan_json:
            if isinstance(node, dict):
                plan_node = node.get("Plan", {})
                if isinstance(plan_node, dict):
                    total += int(plan_node.get("Plan Rows", 0))
        return total
