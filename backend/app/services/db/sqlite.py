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

    def shape(self) -> list[tuple[str, str]]:
        """Every (table, column) in the file — ONE query.

        SQLite has no information_schema; the table-valued ``pragma_table_info``
        joined to ``sqlite_master`` gives the same (table, column) set in one trip.
        Views are excluded to mirror this dialect's own ``list_tables``.
        """
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT m.name, p.name FROM sqlite_master m "
            "JOIN pragma_table_info(m.name) p "
            "WHERE m.type = 'table' AND m.name NOT LIKE 'sqlite_%'"
        )
        return [(str(r[0]), str(r[1])) for r in cur.fetchall()]

    def describe_table(self, table: str) -> list[dict]:
        assert self._conn is not None
        cur = self._conn.execute(f'PRAGMA table_info("{table}")')
        rows = cur.fetchall()
        if not rows:
            return []
        # PRAGMA table_info doesn't expose FK info usefully here; PK is in row row.
        key_roles = self._column_key_roles(table)
        return [
            {
                "name": r[1],
                "type": r[2] or "TEXT",
                "nullable": r[3] == 0,  # notnull=0 → nullable
                "default": r[4],
                "pk": r[5] == 1,
                # SQLite has no COMMENT syntax at all: None is the honest answer,
                # and it is a different fact from "the source declared no comment".
                "comment": None,
                # An INTEGER PRIMARY KEY is rowid and gets no index_list entry —
                # the PK fact comes from table_info, so it is stated here too.
                "key": key_roles.get(str(r[1])) or ("PRI" if r[5] == 1 else ""),
            }
            for r in rows
        ]

    def _column_key_roles(self, table: str) -> dict[str, str]:
        """``{column: 'PRI'|'UNI'|'MUL'}`` from PRAGMA index_list/index_info."""
        assert self._conn is not None
        roles: dict[str, str] = {}
        rank = {"PRI": 3, "UNI": 2, "MUL": 1, "": 0}

        def _record(col: str, role: str) -> None:
            if rank[role] > rank.get(roles.get(col, ""), 0):
                roles[col] = role

        try:
            indexes = self._conn.execute(f'PRAGMA index_list("{table}")').fetchall()
        except Exception:
            logger.debug("sqlite: index_list unavailable for %s", table, exc_info=True)
            return roles
        for idx in indexes:
            # (seq, name, unique, origin, partial) — origin 'pk' marks the PK index.
            name = idx[1]
            unique = bool(idx[2])
            origin = idx[3] if len(idx) > 3 else ""
            role = "PRI" if origin == "pk" else ("UNI" if unique else "MUL")
            try:
                info = self._conn.execute(f'PRAGMA index_info("{name}")').fetchall()
            except Exception:
                continue
            for part in info:
                if part[2]:
                    _record(str(part[2]), role)
        return roles

    def table_facts(self, table: str) -> dict:
        """SQLite reports page counts, not byte sizes or row estimates.

        Rows are deliberately omitted: SQLite has no statistics, and a
        ``COUNT(*)`` here would be the count the indexer already runs.
        """
        assert self._conn is not None
        facts: dict = {}
        try:
            page_count = self._conn.execute("PRAGMA page_count").fetchone()
            page_size = self._conn.execute("PRAGMA page_size").fetchone()
            total = int((page_count or [0])[0] or 0) * int((page_size or [0])[0] or 0)
            if total:
                # Whole-file size: SQLite cannot attribute pages to one table.
                facts["size_bytes"] = total
        except Exception:
            logger.debug("sqlite: page stats unavailable", exc_info=True)
        try:
            kind = self._conn.execute(
                "SELECT type FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()
            if kind and kind[0]:
                facts["table_type"] = "BASE TABLE" if str(kind[0]) == "table" else "VIEW"
        except Exception:
            logger.debug("sqlite: sqlite_master lookup failed for %s", table, exc_info=True)
        return facts

    def get_indexes(self, table: str) -> list[dict]:
        """Index inventory from PRAGMA index_list (+ index_info for columns)."""
        assert self._conn is not None
        out: list[dict] = []
        try:
            indexes = self._conn.execute(f'PRAGMA index_list("{table}")').fetchall()
        except Exception:
            logger.debug("sqlite: index_list unavailable for %s", table, exc_info=True)
            return out
        for idx in indexes:
            name = str(idx[1])
            unique = bool(idx[2])
            origin = idx[3] if len(idx) > 3 else ""
            columns: list[str] = []
            try:
                info = self._conn.execute(f'PRAGMA index_info("{name}")').fetchall()
                columns = [str(p[2]) for p in info if p[2]]
            except Exception:
                columns = []
            out.append(
                {
                    "name": name,
                    "columns": columns,
                    "unique": unique,
                    "primary": origin == "pk",
                }
            )
        return out

    def get_foreign_keys(self, table: str) -> list[dict]:
        """FK constraints from ``PRAGMA foreign_key_list`` — the SAME contract
        as the MySQL connector's ``get_foreign_keys``. SQLite has no schemas,
        so ``ref_schema`` is "".

        2C defect fix (D-1, 2026-10-30): this method did not exist, so the
        catalog indexer's declared-FK discovery silently degraded to [] on
        SQLite — Tier-A acceptance surfaced it (a synthetic schema's inline
        REFERENCES were never indexed).

        When the constraint references the parent's PRIMARY KEY implicitly
        (``to`` is NULL in the pragma), the parent's declared PK column is
        resolved and used — that is what the constraint states, not an
        inference about the data.
        """
        assert self._conn is not None
        out: list[dict] = []
        try:
            rows = self._conn.execute(
                f'PRAGMA foreign_key_list("{table}")'
            ).fetchall()
        except Exception:
            logger.debug(
                "sqlite: foreign_key_list unavailable for %s", table,
                exc_info=True,
            )
            return out
        parent_pk: dict[str, str] = {}
        for r in rows or []:
            ref_table = str(r[2] or "")
            ref_column = r[4]
            if not ref_column:
                pk = parent_pk.get(ref_table)
                if pk is None:
                    try:
                        prow = self._conn.execute(
                            f'PRAGMA table_info("{ref_table}")'
                        ).fetchall()
                        pk = next(
                            (str(p[1]) for p in prow if p[5]), ""
                        )  # p[5] is the pk flag
                    except Exception:
                        logger.debug(
                            "sqlite: parent PK lookup failed for %s",
                            ref_table, exc_info=True,
                        )
                        pk = ""
                    parent_pk[ref_table] = pk
                ref_column = pk
            out.append(
                {
                    "column": str(r[3] or ""),
                    "ref_schema": "",
                    "ref_table": ref_table,
                    "ref_column": str(ref_column or ""),
                }
            )
        return out

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
        # SQLite executes the authored statement verbatim.
        self.last_executed_sql = sql
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
