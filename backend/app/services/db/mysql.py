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
        # Reuse a process-wide pooled engine (per connection signature) so
        # repeated queries don't pay a fresh handshake each time.
        from app.services.db.engine_cache import acquire_engine

        # Hard driver-level timeouts so an unreachable/slow datasource can
        # never hold a pooled connection (and thus an app-pool slot) forever:
        # connect_timeout bounds DNS/TCP/handshake, read/write_timeout bound
        # socket I/O. The per-statement MAX_EXECUTION_TIME hint handles query
        # time, these handle everything else (and MariaDB, which ignores the
        # hint).
        self._engine = acquire_engine(
            self.dialect,
            self._build_url(),
            connect_args={
                "connect_timeout": 15,
                "read_timeout": 60,
                "write_timeout": 60,
            },
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        # The engine is cached for reuse across queries — do NOT dispose it
        # here. Only clear our local reference so the pool stays warm.
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

    def _apply_session_guards(self, conn: Any, timeout_s: int) -> None:
        """Read-only session + per-statement timeout, on every checkout.

        Read-only is the defense-in-depth layer UNDER the SQL text gate: even if
        a write slipped past ``check_read_only_sql``, the server itself refuses
        it. The statement timeout matters most on MariaDB, which ignores the
        ``MAX_EXECUTION_TIME`` hint (MariaDB spells it ``max_statement_time``).

        Both statements are best-effort: an older server that rejects one must
        not break the query.
        """
        stmts = ["SET SESSION TRANSACTION READ ONLY"]
        if timeout_s > 0:
            if (getattr(self.kb, "db_type", "") or "").lower() == "mariadb":
                stmts.append(f"SET SESSION max_statement_time = {int(timeout_s)}")
            else:
                stmts.append(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_s * 1000)}")
        for stmt in stmts:
            try:
                conn.exec_driver_sql(stmt)
            except Exception as e:
                logger.debug("mysql: session guard %r skipped: %s", stmt, e)

    def execute(
        self,
        sql: str,
        params: dict | None = None,
        max_rows: int = 1000,
        timeout_s: int = 10,
    ) -> list[dict]:
        assert self._engine is not None
        # Decide the statement shape BEFORE the hint is prepended — the
        # `/*+ ... */` prefix would otherwise defeat the SELECT check below and
        # silently disable streaming.
        _is_select = _looks_like_select(sql)
        # MySQL supports a per-statement timeout via MAX_EXECUTION_TIME hint
        # (in milliseconds) for SELECTs. We add it for any SELECT-like
        # statement; non-SELECTs ignore the hint safely.
        if timeout_s > 0 and _is_select:
            sql = f"/*+ MAX_EXECUTION_TIME({timeout_s * 1000}) */ " + sql
        # Guards first, on the plain connection.
        with self._engine.connect() as conn:
            self._apply_session_guards(conn, timeout_s)
            # Stream ONLY a SELECT-shaped data statement — never set
            # `stream_results` on the connection itself. Dialects that implement
            # streaming with a server-side cursor (Postgres) wrap EVERY statement
            # in `DECLARE <name> CURSOR WITHOUT HOLD FOR ...`, and
            # `DECLARE ... FOR SET ...` / `... FOR CREATE ...` is a syntax error
            # (live regression, caught by probing a real Postgres KB; pymysql
            # hides it because it swaps in SSCursor at the DBAPI level). The
            # memory bound only matters for result-returning queries anyway.
            _q = (
                conn.execution_options(stream_results=True)
                if _is_select
                else conn
            )
            result = _q.execute(text(sql), params or {})
            if result.returns_rows:
                columns = list(result.keys())
                rows = result.fetchmany(max_rows)
                out = [row_to_dict(columns, tuple(r)) for r in rows]
                result.close()
                return out
            return [{"affected_rows": result.rowcount}]

    def get_foreign_keys(self, table: str) -> list[dict]:
        """Return FK constraints for a table.

        Returns:
            [{"column": "product_id", "ref_table": "products",
              "ref_schema": "aipdp", "ref_column": "id"}, ...]
        """
        assert self._engine is not None
        sql = text(
            "SELECT kcu.COLUMN_NAME, kcu.REFERENCED_TABLE_SCHEMA, "
            "       kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE kcu "
            "WHERE kcu.TABLE_SCHEMA = DATABASE() "
            "  AND kcu.TABLE_NAME = :t "
            "  AND kcu.REFERENCED_TABLE_NAME IS NOT NULL"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"t": table}).fetchall()
        return [
            {
                "column": r[0],
                "ref_schema": r[1] or "",
                "ref_table": r[2],
                "ref_column": r[3],
            }
            for r in rows
        ]

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
