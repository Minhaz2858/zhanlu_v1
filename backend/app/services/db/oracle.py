"""Oracle connector using SQLAlchemy + cx-Oracle.

Note: cx-Oracle must be installed (see requirements.txt). For testing
without Oracle available, SQLite is the fallback fixture.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.services.db.base import BaseConnector, row_to_dict

logger = logging.getLogger(__name__)


class OracleConnector:
    """Connector for Oracle (uses cx_Oracle driver)."""

    def __init__(self, kb: Any):
        self.kb = kb
        self.dialect = "oracle"
        self._engine: Engine | None = None

    def _build_url(self) -> str:
        from urllib.parse import quote_plus
        user = self.kb.username or "system"
        pwd = self.kb.password or ""
        host = self.kb.host or "localhost"
        port = int(self.kb.port or 1521)
        db = self.kb.database_name or "ORCL"
        # URL-encode user/password so special chars (e.g. `@` in the password)
        # aren't interpreted as the user/host separator by SQLAlchemy.
        user_enc = quote_plus(user)
        pwd_enc = quote_plus(pwd)
        return f"oracle+cx_oracle://{user_enc}:{pwd_enc}@{host}:{port}/?service_name={db}"

    def __enter__(self) -> "OracleConnector":
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
            "SELECT table_name FROM user_tables ORDER BY table_name"
        )
        with self._engine.connect() as conn:
            return [r[0].lower() for r in conn.execute(sql).fetchall()]

    def describe_table(self, table: str) -> list[dict]:
        assert self._engine is not None
        table_up = table.upper()
        sql = text(
            "SELECT column_name, data_type, nullable, data_default "
            "FROM user_tab_columns WHERE table_name = :t ORDER BY column_id"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"t": table_up}).fetchall()
        return [
            {
                "name": r[0].lower(),
                "type": r[1],
                "nullable": (r[2] or "").upper() == "Y",
                "default": r[3],
                "pk": False,  # PK detection requires all_constraints join; kept simple in v1
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
        # SQLcl-style hint; works on modern Oracle releases.
        wrapped = (
            f"SELECT * FROM ({sql}) WHERE ROWNUM <= {int(max_rows)}"
            if not _has_row_limit(sql)
            else sql
        )
        with self._engine.connect() as conn:
            result = conn.execute(text(wrapped), params or {})
            if result.returns_rows:
                columns = list(result.keys())
                rows = result.fetchmany(max_rows)
                return [row_to_dict(columns, tuple(r)) for r in rows]
            return [{"affected_rows": result.rowcount}]

    def test_connection(self) -> dict:
        try:
            with self:
                with self._engine.connect() as conn:
                    v = conn.execute(text("SELECT BANNER FROM V$VERSION WHERE ROWNUM=1")).scalar()
            return {"ok": True, "info": f"Oracle {v}"}
        except Exception as e:
            return {"ok": False, "info": str(e)}


def _has_row_limit(sql: str) -> bool:
    return "rownum" in sql.lower() or "fetch first" in sql.lower() or "limit " in sql.lower()
