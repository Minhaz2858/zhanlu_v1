"""MSSQL connector using SQLAlchemy + pymssql."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.services.db.base import BaseConnector, row_to_dict

logger = logging.getLogger(__name__)


class MSSQLConnector:
    """Connector for Microsoft SQL Server (uses pymssql driver)."""

    def __init__(self, kb: Any):
        self.kb = kb
        self.dialect = "mssql"
        self._engine: Engine | None = None

    def _build_url(self) -> str:
        from urllib.parse import quote_plus
        user = self.kb.username or "sa"
        pwd = self.kb.password or ""
        host = self.kb.host or "localhost"
        port = int(self.kb.port or 1433)
        db = self.kb.database_name or "master"
        # URL-encode user/password so special chars (e.g. `@` in the password)
        # aren't interpreted as the user/host separator by SQLAlchemy.
        user_enc = quote_plus(user)
        pwd_enc = quote_plus(pwd)
        return f"mssql+pymssql://{user_enc}:{pwd_enc}@{host}:{port}/{db}"

    def __enter__(self) -> "MSSQLConnector":
        # Reuse a process-wide pooled engine (per connection signature) so
        # repeated queries don't pay a fresh handshake each time.
        from app.services.db.engine_cache import acquire_engine

        self._engine = acquire_engine(self.dialect, self._build_url())
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        # The engine is cached for reuse across queries — do NOT dispose it
        # here. Only clear our local reference so the pool stays warm.
        self._engine = None

    def list_tables(self) -> list[str]:
        assert self._engine is not None
        sql = text(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
        )
        with self._engine.connect() as conn:
            return [r[0] for r in conn.execute(sql).fetchall()]

    def describe_table(self, table: str) -> list[dict]:
        assert self._engine is not None
        cols_sql = text(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
        )
        pk_sql = text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
            "WHERE TABLE_NAME = :t AND OBJECTPROPERTY(OBJECT_ID(CONSTRAINT_SCHEMA + '.' + CONSTRAINT_NAME), 'IsPrimaryKey') = 1"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(cols_sql, {"t": table}).fetchall()
            pks = {r[0] for r in conn.execute(pk_sql, {"t": table}).fetchall()}
        return [
            {
                "name": r[0],
                "type": r[1],
                "nullable": (r[2] or "").upper() == "YES",
                "default": r[3],
                "pk": r[0] in pks,
            }
            for r in rows
        ]

    def execute(
        self,
        sql: str,
        params: dict | None = None,
        max_rows: int = 1000,
        timeout_s: int = 10,
    ) -> list[dict]:
        assert self._engine is not None
        with self._engine.connect() as conn:
            if timeout_s > 0:
                # MSSQL per-statement timeout (seconds) — applies to read
                # locks but is a reasonable safety net.
                conn.execute(text(f"SET LOCK_TIMEOUT {int(timeout_s * 1000)}"))
            result = conn.execute(text(sql), params or {})
            if result.returns_rows:
                columns = list(result.keys())
                rows = result.fetchmany(max_rows)
                return [row_to_dict(columns, tuple(r)) for r in rows]
            return [{"affected_rows": result.rowcount}]

    def test_connection(self) -> dict:
        try:
            with self:
                with self._engine.connect() as conn:
                    v = conn.execute(text("SELECT @@VERSION")).scalar()
            return {"ok": True, "info": f"MSSQL {v[:60]}"}
        except Exception as e:
            return {"ok": False, "info": str(e)}
