"""SQLite connector (stdlib only — no extra driver)."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from app.services.db.base import BaseConnector, row_to_dict

logger = logging.getLogger(__name__)


class SQLiteConnector:
    """Connector for SQLite. Uses `file_type='file_url'` or `api_url` for path."""

    def __init__(self, kb: Any):
        self.kb = kb
        self.dialect = "sqlite"
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "SQLiteConnector":
        # `api_url` doubles as the file path for SQLite, falling back to :memory:.
        path = (self.kb.api_url or self.kb.file_url or ":memory:").strip()
        if not path:
            path = ":memory:"
        # `check_same_thread=False` so the connection is safe to pass into
        # `asyncio.to_thread()` for short-lived async exec.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as e:
                logger.debug("SQLite close failed: %s", e)
            self._conn = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_tables(self) -> list[str]:
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [r[0] for r in cur.fetchall()]

    def describe_table(self, table: str) -> list[dict]:
        assert self._conn is not None
        cur = self._conn.execute(f'PRAGMA table_info("{table}")')
        rows = cur.fetchall()
        if not rows:
            return []
        # PRAGMA table_info doesn't expose FK info usefully here; PK is in row row.
        return [
            {
                "name": r[1],
                "type": r[2] or "TEXT",
                "nullable": r[3] == 0,  # notnull=0 → nullable
                "default": r[4],
                "pk": r[5] == 1,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(
        self,
        sql: str,
        params: dict | None = None,
        max_rows: int = 1000,
        timeout_s: int = 10,
    ) -> list[dict]:
        assert self._conn is not None
        # SQLite has no per-statement timeout; emulate by limiting wall time
        # via the progress handler.
        if timeout_s > 0:
            def _progress() -> int:
                return 1  # 0=continue, 1=stop (called every N instructions)
            self._conn.set_progress_handler(_progress, timeout_s * 1000)
        try:
            cur = self._conn.execute(sql, params or {})
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows)
            return [row_to_dict(columns, tuple(r)) for r in rows]
        finally:
            try:
                self._conn.set_progress_handler(None, 0)
            except Exception:
                pass

    def test_connection(self) -> dict:
        try:
            with self:
                cur = self._conn.execute("SELECT sqlite_version() AS v")
                version = cur.fetchone()[0]
                return {"ok": True, "info": f"SQLite {version}"}
        except Exception as e:
            return {"ok": False, "info": str(e)}
