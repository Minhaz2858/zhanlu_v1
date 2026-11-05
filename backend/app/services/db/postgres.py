"""PostgreSQL connector using SQLAlchemy + psycopg2-binary."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.services.db.base import BaseConnector, row_to_dict

logger = logging.getLogger(__name__)


def _looks_like_select(sql: str) -> bool:
    """True for statements that return a result set (so streaming is meaningful).

    Mirrors the MySQL connector's helper: only these may be wrapped in a
    server-side cursor.
    """
    head = (sql or "").lstrip().lower()
    return head.startswith("select") or head.startswith("with")


class PostgresConnector:
    """Connector for PostgreSQL."""

    def __init__(self, kb: Any):
        self.kb = kb
        self.dialect = "postgres"
        self._engine: Engine | None = None
        self._schema = getattr(kb, "schema", None) or "public"

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

    def _search_path_option(self) -> str | None:
        """Return a libpq ``options`` value pinning ``search_path`` to the KB
        schema, or ``None`` when the default (``public``) is in effect.

        Quoting the identifier defensively (doubling embedded quotes) prevents
        the schema name from being interpreted as an injection vector while
        still allowing schema names with mixed case or special characters.
        """
        if not self._schema or self._schema == "public":
            return None
        safe = self._schema.replace('"', '""')
        return f'-csearch_path="{safe}"'

    def __enter__(self) -> "PostgresConnector":
        # Reuse a process-wide pooled engine (per connection signature) so
        # repeated queries don't pay a fresh handshake each time.
        from app.services.db.engine_cache import acquire_engine

        connect_args: dict[str, Any] = {}
        opt = self._search_path_option()
        if opt:
            connect_args["options"] = opt
        # psycopg2 connect_timeout (seconds) so an unreachable datasource
        # fails fast instead of holding a pooled connection (and an app-pool
        # slot) for the OS TCP timeout.
        connect_args.setdefault("connect_timeout", 15)
        self._engine = acquire_engine(
            self.dialect, self._build_url(), connect_args=connect_args
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
        sql = text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :schema "
            "AND table_type = 'BASE TABLE' ORDER BY table_name"
        )
        with self._engine.connect() as conn:
            return [r[0] for r in conn.execute(sql, {"schema": self._schema}).fetchall()]

    def shape(self) -> list[tuple[str, str]]:
        """Every (table, column) in the current schema — ONE round trip."""
        assert self._engine is not None
        sql = text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = :schema"
        )
        with self._engine.connect() as conn:
            return [
                (str(r[0]), str(r[1]))
                for r in conn.execute(sql, {"schema": self._schema}).fetchall()
            ]

    def describe_table(self, table: str) -> list[dict]:
        assert self._engine is not None
        sql = text(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :t "
            "ORDER BY ordinal_position"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"schema": self._schema, "t": table}).fetchall()
        # Pull PKs separately. Qualify with the schema so the regclass lookup
        # doesn't rely on search_path (a table in a non-default schema would
        # otherwise be missed or resolve to a same-named table elsewhere).
        pk_sql = text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = (:schema || '.' || :t)::regclass AND i.indisprimary"
        )
        with self._engine.connect() as conn:
            pks = {r[0] for r in conn.execute(pk_sql, {"schema": self._schema, "t": table}).fetchall()}
        # Column comments live in pg_description, not in information_schema —
        # they are a second query for the same reason the PK lookup is.
        comment_sql = text(
            "SELECT a.attname, d.description FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_description d ON d.objoid = a.attrelid "
            "     AND d.objsubid = a.attnum "
            "WHERE n.nspname = :schema AND c.relname = :t "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        )
        with self._engine.connect() as conn:
            comments = {
                r[0]: r[1]
                for r in conn.execute(comment_sql, {"schema": self._schema, "t": table}).fetchall()
            }
        # Index membership per column (primary / unique / plain), same shape the
        # MySQL connector gets for free from information_schema.COLUMNS.COLUMN_KEY.
        key_roles = self._column_key_roles(table)
        return [
            {
                "name": r[0],
                "type": r[1],
                "nullable": (r[2] or "").lower() == "yes",
                "default": r[3],
                "pk": r[0] in pks,
                "comment": (comments.get(r[0]) or "").strip() or None,
                "key": key_roles.get(r[0], ""),
            }
            for r in rows
        ]

    def _column_key_roles(self, table: str) -> dict[str, str]:
        """``{column: 'PRI'|'UNI'|'MUL'}`` — strongest role wins per column."""
        assert self._engine is not None
        sql = text(
            "SELECT a.attname, i.indisprimary, i.indisunique FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE n.nspname = :schema AND c.relname = :t "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        )
        rank = {"PRI": 3, "UNI": 2, "MUL": 1, "": 0}
        roles: dict[str, str] = {}
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(sql, {"schema": self._schema, "t": table}).fetchall()
        except Exception:
            logger.debug("postgres: key roles unavailable for %s", table, exc_info=True)
            return roles
        for name, is_primary, is_unique in rows:
            role = "PRI" if is_primary else ("UNI" if is_unique else "MUL")
            if rank[role] > rank.get(roles.get(str(name), ""), 0):
                roles[str(name)] = role
        return roles

    def table_facts(self, table: str) -> dict:
        """Engine-reported facts about one relation (never its rows).

        ``reltuples`` is the planner's estimate (updated by ANALYZE/VACUUM), so
        the caller labels it ``estimated``. ``relpages * block_size`` is the
        on-disk allocation.
        """
        assert self._engine is not None
        sql = text(
            "SELECT c.relkind, c.reltuples::bigint, "
            "       (c.relpages * current_setting('block_size')::int)::bigint, "
            "       obj_description(c.oid, 'pg_class') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :t"
        )
        with self._engine.connect() as conn:
            row = conn.execute(sql, {"schema": self._schema, "t": table}).fetchone()
        if row is None:
            return {}
        kind = {"r": "BASE TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW", "p": "BASE TABLE"}
        facts: dict = {}
        if row[0]:
            facts["table_type"] = kind.get(str(row[0]), str(row[0]))
        comment = (row[3] or "").strip()
        if comment:
            facts["comment"] = comment
        est = row[1]
        if est is not None and int(est) >= 0:
            facts["est_rows"] = int(est)
        if row[2] is not None and int(row[2]) > 0:
            facts["size_bytes"] = int(row[2])
        return facts

    def get_indexes(self, table: str) -> list[dict]:
        """Index inventory from pg_index (columns resolved in index order).

        ``indisunique`` / ``indisprimary`` live on ``pg_index`` (alias ``ix``),
        NOT on the index's own ``pg_class`` row (alias ``i``, which only carries
        the index name). Reading them off ``i`` fails the whole statement, and
        the catalog then records "indexes not collected" for every PostgreSQL
        source — so the alias matters.
        """
        assert self._engine is not None
        sql = text(
            "SELECT i.relname, ix.indisunique, ix.indisprimary, "
            "       array_to_string(array(SELECT a.attname FROM unnest(ix.indkey) WITH ORDINALITY "
            "                                AS k(attnum, ord) "
            "                                JOIN pg_attribute a ON a.attrelid = ix.indrelid "
            "                                                     AND a.attnum = k.attnum "
            "                                ORDER BY k.ord), ',') "
            "FROM pg_index ix "
            "JOIN pg_class c ON c.oid = ix.indrelid "
            "JOIN pg_class i ON i.oid = ix.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :t"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"schema": self._schema, "t": table}).fetchall()
        return [
            {
                "name": str(r[0]),
                "columns": [c for c in (r[3] or "").split(",") if c],
                "unique": bool(r[1]),
                "primary": bool(r[2]),
            }
            for r in rows
        ]

    def get_foreign_keys(self, table: str) -> list[dict]:
        """FK constraints for one relation — the SAME contract as the MySQL
        connector's ``get_foreign_keys``.

        2C defect fix (D-1, 2026-10-30): this method did not exist on the
        Postgres connector, so declared-FK discovery silently degraded to []
        on PostgreSQL — Tier-A acceptance surfaced it.

        2C Tier-B fix (2026-10-31): the first implementation read
        ``information_schema.constraint_column_usage``, which PostgreSQL
        exposes ONLY to the constraint's owner — a read-only customer
        account (the typical customer connection) saw NO declared FKs at
        all. The live Tier-B run measured exactly that. The catalog is now
        read from ``pg_catalog`` (``pg_constraint``), which is readable by
        any role that can SELECT the table.
        """
        assert self._engine is not None
        sql = text(
            "SELECT sa.attname AS column_name, "
            "       rns.nspname AS ref_schema, "
            "       rc.relname AS ref_table, "
            "       ra.attname AS ref_column "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace ns ON ns.oid = c.relnamespace "
            "JOIN pg_class rc ON rc.oid = con.confrelid "
            "JOIN pg_namespace rns ON rns.oid = rc.relnamespace "
            "JOIN unnest(con.conkey) WITH ORDINALITY AS ck(attnum, ord) "
            "  ON true "
            "JOIN unnest(con.confkey) WITH ORDINALITY AS fk(attnum, ord) "
            "  ON fk.ord = ck.ord "
            "JOIN pg_attribute sa "
            "  ON sa.attrelid = con.conrelid AND sa.attnum = ck.attnum "
            "JOIN pg_attribute ra "
            "  ON ra.attrelid = con.confrelid AND ra.attnum = fk.attnum "
            "WHERE con.contype = 'f' "
            "  AND ns.nspname = :schema AND c.relname = :t "
            "ORDER BY con.oid, ck.ord"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(
                sql, {"schema": self._schema, "t": table}
            ).fetchall()
        return [
            {
                "column": r[0],
                "ref_schema": r[1] or "",
                "ref_table": r[2],
                "ref_column": r[3],
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
        # Postgres executes the authored statement as written (the timeout is a
        # session SET, not a prefix), so this is the executable form verbatim.
        self.last_executed_sql = sql
        with self._engine.connect() as conn:
            # Guards first, on the plain connection: a read-only session is the
            # defense-in-depth layer under the SQL text gate (even a write that
            # slipped past `check_read_only_sql` is refused by the server), and
            # `SET LOCAL statement_timeout` bounds this transaction.
            #
            # TWO read-only statements are required: `default_transaction_read_only`
            # only governs transactions started AFTER it, so the transaction this
            # very statement opened would stay writable — and `SET LOCAL
            # transaction_read_only` closes exactly that hole.
            for _guard in (
                "SET default_transaction_read_only = on",
                "SET LOCAL transaction_read_only = on",
            ):
                try:
                    conn.exec_driver_sql(_guard)
                except Exception as e:
                    logger.debug("postgres: session guard %r skipped: %s", _guard, e)
            if timeout_s > 0:
                conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_s * 1000)}"))
            else:
                # 2026-10-10 (timeout policy round 2): timeout DISABLED —
                # explicitly zero the limit. Skipping the SET let a server-
                # global statement_timeout inherit into the session, killing
                # heavy aggregations despite QUERY_TIMEOUT_DISABLED=True
                # (the MySQL 3024 equivalent: "canceling statement due to
                # statement timeout").
                conn.execute(text("SET LOCAL statement_timeout = 0"))
            # Stream ONLY a SELECT-shaped statement. `stream_results` must never
            # be set on the connection itself: SQLAlchemy then wraps EVERY
            # statement in `DECLARE <name> CURSOR WITHOUT HOLD FOR ...`, and
            # `DECLARE ... FOR SET LOCAL ...` (or FOR CREATE) is a syntax error —
            # which broke every Postgres query until the live probe caught it.
            # Non-SELECT statements are rare here (reads are enforced upstream)
            # and gain nothing from streaming.
            _q = (
                conn.execution_options(stream_results=True)
                if _looks_like_select(sql)
                else conn
            )
            result = _q.execute(text(sql), params or {})
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
