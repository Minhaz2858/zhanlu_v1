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
        connect_args: dict[str, Any] = {
            "connect_timeout": 15,
        }
        # Socket read/write timeouts are a transport cap, not a policy: when
        # QUERY_TIMEOUT_DISABLED removes the statement timeout, these must go
        # too — a month-scale aggregation legitimately runs past 60s.
        try:
            from app.config import settings as _s

            _timeout_disabled = getattr(_s, "QUERY_TIMEOUT_DISABLED", False)
        except Exception:  # noqa: BLE001
            _timeout_disabled = False
        if not _timeout_disabled:
            connect_args["read_timeout"] = 60
            connect_args["write_timeout"] = 60
        self._engine = acquire_engine(
            self.dialect,
            self._build_url(),
            connect_args=connect_args,
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

    def shape(self) -> list[tuple[str, str]]:
        """Every (table, column) in the current database — ONE round trip.

        Measured on the 143-table / 2,643-column warehouse: ~0.9-1.1 s, the same
        order as ``list_tables()`` and ~25x cheaper than describe_table per table.
        """
        assert self._engine is not None
        sql = text(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()"
        )
        with self._engine.connect() as conn:
            return [(str(r[0]), str(r[1])) for r in conn.execute(sql).fetchall()]

    def describe_table(self, table: str) -> list[dict]:
        assert self._engine is not None
        sql = text(
            "SELECT COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
            "       COLUMN_KEY, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
            "ORDER BY ORDINAL_POSITION"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"t": table}).fetchall()
        return [
            {
                "name": r[0],
                # COLUMN_TYPE, not DATA_TYPE. DATA_TYPE drops the declared
                # length, and on MySQL that is the ONLY way to tell a real
                # binary payload from ``varbinary(0)`` — the marker MySQL writes
                # for a VIEW column whose expression it cannot resolve. Live,
                # v_sale_order's instock_qty/produce_qty/plan_qty are
                # declared varbinary(0) and were unreadable as a result, because
                # the length was thrown away right here. COLUMN_TYPE is also
                # simply the truer declaration: varchar(64), decimal(14,2),
                # int(10) unsigned, and the enum's own values for an ENUM.
                "type": r[1] or r[2],
                "nullable": (r[3] or "").upper() == "YES",
                "default": r[4],
                "pk": (r[5] or "").upper() == "PRI",
                # The source's own words. MySQL writes a label here whenever a
                # COMMENT was declared, which for an ERP is the only place the
                # business meaning exists — it used to be SELECTed nowhere.
                "comment": (r[6] or "").strip() or None,
                # PRI | UNI | MUL — index membership. Only 'PRI' used to be
                # kept (as a bool); UNI/MUL came back and was thrown away.
                "key": (r[5] or "").upper(),
            }
            for r in rows
        ]

    def table_facts(self, table: str) -> dict:
        """Engine-reported facts about one table (never its rows).

        ``TABLE_ROWS`` is InnoDB's statistic, not a count — the caller labels it
        ``estimated``. Views carry NULL there, which is why it is omitted rather
        than coerced to 0.
        """
        assert self._engine is not None
        sql = text(
            "SELECT TABLE_TYPE, ENGINE, TABLE_COMMENT, TABLE_ROWS, "
            "       DATA_LENGTH, INDEX_LENGTH "
            "FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
        )
        with self._engine.connect() as conn:
            row = conn.execute(sql, {"t": table}).fetchone()
        if row is None:
            return {}
        facts: dict = {}
        if row[0]:
            facts["table_type"] = str(row[0])
        if row[1]:
            facts["engine"] = str(row[1])
        comment = (row[2] or "").strip()
        if comment and comment.upper() != "VIEW":
            facts["comment"] = comment
        if row[3] is not None:
            facts["est_rows"] = int(row[3])
        size = sum(int(v or 0) for v in (row[4], row[5]))
        if size:
            facts["size_bytes"] = size
        return facts

    def get_indexes(self, table: str) -> list[dict]:
        """Index inventory from information_schema.STATISTICS (ordered parts)."""
        assert self._engine is not None
        sql = text(
            "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"t": table}).fetchall()
        by_name: dict[str, dict] = {}
        for name, non_unique, _seq, column in rows:
            entry = by_name.setdefault(
                str(name),
                {"name": str(name), "columns": [], "unique": not non_unique, "primary": False},
            )
            if column:
                entry["columns"].append(str(column))
        for entry in by_name.values():
            entry["primary"] = entry["name"] == "PRIMARY"
        return list(by_name.values())

    def _apply_session_guards(self, conn: Any, timeout_s: int) -> None:
        """Read-only session + per-statement timeout, on every checkout.

        Read-only is the defense-in-depth layer UNDER the SQL text gate: even if
        a write slipped past ``check_read_only_sql``, the server itself refuses
        it. The statement timeout matters most on MariaDB, which ignores the
        ``MAX_EXECUTION_TIME`` hint (MariaDB spells it ``max_statement_time``).

        Both statements are best-effort: an older server that rejects one must
        not break the query.

        2026-10-10 (timeout policy round 2): when the timeout policy is
        DISABLED (timeout_s <= 0) we now EXPLICITLY zero the session limit.
        The old behavior — skipping the SET — let the MySQL server's GLOBAL
        ``max_execution_time`` inherit into the session, so heavy aggregations
        still died with error 3024 ("maximum statement execution time
        exceeded") even with QUERY_TIMEOUT_DISABLED=True. Live: the
        June–August customer comparison query died on exactly that.
        """
        stmts = ["SET SESSION TRANSACTION READ ONLY"]
        if timeout_s > 0:
            if (getattr(self.kb, "db_type", "") or "").lower() == "mariadb":
                stmts.append(f"SET SESSION max_statement_time = {int(timeout_s)}")
            else:
                stmts.append(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_s * 1000)}")
        else:
            if (getattr(self.kb, "db_type", "") or "").lower() == "mariadb":
                stmts.append("SET SESSION max_statement_time = 0")
            else:
                stmts.append("SET SESSION MAX_EXECUTION_TIME = 0")
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
        # The executable form: the authored statement plus this dialect's own
        # prefix. Recorded so provenance can show what the database actually ran
        # (`COUNT(*)` stays a valid call; the hint is not part of the question).
        self.last_executed_sql = sql
        # Guards first, on the plain connection.
        try:
            return self._execute_with_guards(sql, params, max_rows, timeout_s, _is_select)
        except Exception as e:
            # 2026-10-10 self-heal: error 3024 = the SERVER's statement timeout
            # fired despite our session zeroing (e.g. a global resource limit a
            # pooled connection inherited mid-flight, or a proxy in the path).
            # Retry ONCE on a fresh checkout with the limit explicitly zeroed
            # before the statement. Never retries when a timeout policy is
            # actually configured (timeout_s > 0) — that timeout is intentional.
            _msg = str(e).lower()
            if (
                timeout_s <= 0
                and ("3024" in _msg or "maximum statement execution time" in _msg)
            ):
                logger.warning(
                    "mysql: server-side statement timeout (3024) despite "
                    "disabled policy — zeroing session limit and retrying once"
                )
                with self._engine.connect() as conn:
                    _zero = (
                        "SET SESSION max_statement_time = 0"
                        if (getattr(self.kb, "db_type", "") or "").lower() == "mariadb"
                        else "SET SESSION MAX_EXECUTION_TIME = 0"
                    )
                    try:
                        conn.exec_driver_sql(_zero)
                    except Exception as guard_err:
                        logger.debug("mysql: 3024 heal guard skipped: %s", guard_err)
                    self.last_executed_sql = sql
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
            raise

    def _execute_with_guards(self, sql: str, params: dict | None, max_rows: int, timeout_s: int, _is_select: bool) -> list[dict]:
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
              "ref_schema": "acme", "ref_column": "id"}, ...]
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
