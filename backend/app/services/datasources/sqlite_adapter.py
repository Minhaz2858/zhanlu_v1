"""SQLite datasource adapter — stdlib ``sqlite3`` with progress-handler watchdog."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from app.services.datasources import (
    ColumnInfo,
    DatasourceAdapter,
    ExplainResult,
    QueryResult,
)

logger = logging.getLogger(__name__)


class SQLiteAdapter:
    """SQLite adapter satisfying the ``DatasourceAdapter`` Protocol."""

    quote_char = '"'

    def __init__(self, db_path: str = ":memory:", *, timeout_ms: int = 5000):
        self._db_path = db_path
        self._timeout_ms = timeout_ms
        self._conn: sqlite3.Connection | None = None

    # ── helpers ───────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _set_pragma_timeout(self, conn: sqlite3.Connection) -> None:
        conn.execute(f"PRAGMA query_only=ON")         # read-only enforcement
        conn.set_progress_handler(self._progress_handler, 1000)

    @staticmethod
    def _progress_handler() -> int:
        return 0  # 0 = abort (handled in query); caller sets a deadline

    # ── Protocol methods ──────────────────────────────────────────────

    def test_connection(self) -> bool:
        try:
            self._get_conn().execute("SELECT 1")
            return True
        except Exception as e:
            logger.warning("SQLite connection test failed: %s", e)
            return False

    def list_tables(self) -> list[str]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def describe_table(self, table: str) -> list[ColumnInfo]:
        conn = self._get_conn()
        rows = conn.execute(f"PRAGMA table_info({self._quote_ident(table)})").fetchall()
        pks: set[int] = set()
        pk_rows = conn.execute(f"PRAGMA table_info({self._quote_ident(table)})").fetchall()
        for r in pk_rows:
            if r["pk"]:
                pks.add(r["cid"])
        result: list[ColumnInfo] = []
        for r in rows:
            result.append(ColumnInfo(
                name=r["name"],
                dtype=r["type"] or "TEXT",
                nullable=not bool(r["notnull"]),
                default=r["dflt_value"],
                is_pk=r["cid"] in pks,
            ))
        return result

    def refresh_schema(self) -> dict[str, list[ColumnInfo]]:
        tables = self.list_tables()
        return {t: self.describe_table(t) for t in tables}

    def explain(self, sql: str, params: dict[str, Any] | None = None) -> ExplainResult:
        conn = self._get_conn()
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params or {}).fetchall()
        plan_text = "\n".join(str(dict(r)) for r in rows)
        # Approximate cost: count the number of SCAN steps
        cost = sum(1 for r in rows if "SCAN" in str(dict(r)))
        est_rows = self._estimate_rows_from_plan(rows)
        return ExplainResult(
            plan_json=[dict(r) for r in rows],
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
        deadline = time.monotonic() + (timeout_ms / 1000.0)

        def watchdog() -> int:
            if time.monotonic() > deadline:
                return 0  # abort
            return 1  # continue

        conn.set_progress_handler(watchdog, 100)
        try:
            limited_sql = f"{sql.rstrip(';')} LIMIT {int(row_limit)}"
            t0 = time.monotonic()
            cur = conn.execute(limited_sql, params or {})
            rows = cur.fetchall()
            elapsed = (time.monotonic() - t0) * 1000
            cols = [d[0] for d in cur.description] if cur.description else []
            return QueryResult(
                columns=cols,
                rows=[tuple(r) for r in rows],
                row_count=len(rows),
                duration_ms=round(elapsed, 2),
            )
        except Exception:
            raise
        finally:
            conn.set_progress_handler(None, 0)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── internal ──────────────────────────────────────────────────────

    @staticmethod
    def _quote_ident(name: str) -> str:
        return f'"{name}"'

    @staticmethod
    def _estimate_rows_from_plan(rows: list[sqlite3.Row]) -> int:
        total = 0
        for r in rows:
            d = dict(r)
            detail = str(d.get("detail", ""))
            # SQLite QP sometimes includes "~N rows" in the detail
            import re
            m = re.search(r"~(\d+)\s+rows", detail)
            if m:
                total += int(m.group(1))
        return total or 100  # fallback
