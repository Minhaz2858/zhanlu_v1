"""MySQL connector using SQLAlchemy + pymysql."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.services.db.base import BaseConnector, row_to_dict

logger = logging.getLogger(__name__)


class MySQLConnector:
    """Connector for MySQL/MariaDB."""

    def __init__(self, kb: Any):
        self.kb = kb
        self.dialect = "mysql"
        self._engine: Engine | None = None

    def _build_url(self) -> str:
        from urllib.parse import quote_plus
        user = self.kb.username or "root"
        pwd = self.kb.password or ""
        host = self.kb.host or "localhost"
        port = int(self.kb.port or 3306)
        db = self.kb.database_name or ""
        # URL-encode user/password so special chars (e.g. `@` in the password)
        # aren't interpreted as the user/host separator by SQLAlchemy.
        user_enc = quote_plus(user)
        pwd_enc = quote_plus(pwd)
        return (
            f"mysql+pymysql://{user_enc}:{pwd_enc}@{host}:{port}/{db}?charset=utf8mb4"
        )

    def __enter__(self) -> "MySQLConnector":
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
                logger.debug("MySQL engine dispose failed: %s", e)
            self._engine = None

    def list_tables(self) -> list[str]:
        assert self._engine is not None
        sql = (
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
        )
        with self._engine.connect() as conn:
            return [r[0] for r in conn.execute(text(sql)).fetchall()]

    def describe_table(self, table: str) -> list[dict]:
        assert self._engine is not None
        sql = text(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
            "       COLUMN_KEY "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
            "ORDER BY ORDINAL_POSITION"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"t": table}).fetchall()
        return [
            {
                "name": r[0],
                "type": r[1],
                "nullable": (r[2] or "").upper() == "YES",
                "default": r[3],
                "pk": (r[4] or "").upper() == "PRI",
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
        # MySQL supports a per-statement timeout via MAX_EXECUTION_TIME hint
        # (in milliseconds) for SELECTs. We add it for any SELECT-like
        # statement; non-SELECTs ignore the hint safely.
        if timeout_s > 0 and _looks_like_select(sql):
            sql = f"/*+ MAX_EXECUTION_TIME({timeout_s * 1000}) */ " + sql
        with self._engine.connect() as conn:
            result = conn.execute(text(sql), params or {})
            if result.returns_rows:
                columns = list(result.keys())
                rows = result.fetchmany(max_rows)
                return [row_to_dict(columns, tuple(r)) for r in rows]
            return [{"affected_rows": result.rowcount}]

    def test_connection(self) -> dict:
        try:
            with self:
                engine = self._engine
                with engine.connect() as conn:
                    v = conn.execute(text("SELECT VERSION()")).scalar()
            return {"ok": True, "info": f"MySQL {v}"}
        except Exception as e:
            return {"ok": False, "info": str(e)}


def _looks_like_select(sql: str) -> bool:
    head = sql.lstrip().lower()
    return head.startswith("select") or head.startswith("with")
