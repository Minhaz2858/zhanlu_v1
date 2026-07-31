"""PostgreSQL connector using SQLAlchemy + psycopg2-binary."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.services.db.base import BaseConnector, row_to_dict

logger = logging.getLogger(__name__)


class PostgresConnector:
    """Connector for PostgreSQL."""

    def __init__(self, kb: Any):
        self.kb = kb
        self.dialect = "postgres"
        self._engine: Engine | None = None

    def _build_url(self) -> str:
        from urllib.parse import quote_plus
        user = self.kb.username or "postgres"
        pwd = self.kb.password or ""
        host = self.kb.host or "localhost"
        port = int(self.kb.port or 5432)
        db = self.kb.database_name or "postgres"
        # URL-encode user/password so special chars (e.g. `@` in the password)
        # aren't interpreted as the user/host separator by SQLAlchemy.
        user_enc = quote_plus(user)
        pwd_enc = quote_plus(pwd)
        return f"postgresql+psycopg2://{user_enc}:{pwd_enc}@{host}:{port}/{db}"

    def __enter__(self) -> "PostgresConnector":
        self._engine = create_engine(
            self._build_url(),
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True,
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.dispose()
            except Exception as e:
                logger.debug("Postgres engine dispose failed: %s", e)
            self._engine = None

    def list_tables(self) -> list[str]:
        assert self._engine is not None
        sql = text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() "
            "AND table_type = 'BASE TABLE' ORDER BY table_name"
        )
        with self._engine.connect() as conn:
            return [r[0] for r in conn.execute(sql).fetchall()]

    def describe_table(self, table: str) -> list[dict]:
        assert self._engine is not None
        sql = text(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t "
            "ORDER BY ordinal_position"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"t": table}).fetchall()
        # Pull PKs separately
        pk_sql = text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = (:t)::regclass AND i.indisprimary"
        )
        with self._engine.connect() as conn:
            pks = {r[0] for r in conn.execute(pk_sql, {"t": table}).fetchall()}
        return [
            {
                "name": r[0],
                "type": r[1],
                "nullable": (r[2] or "").lower() == "yes",
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
                conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_s * 1000)}"))
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
                    v = conn.execute(text("SELECT version()")).scalar()
            return {"ok": True, "info": f"PostgreSQL {v.split(',')[0]}"}
        except Exception as e:
            return {"ok": False, "info": str(e)}
