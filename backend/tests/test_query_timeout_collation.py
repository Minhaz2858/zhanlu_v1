"""Timeout-policy removal + collation self-heal tests (2026-10-10)."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.services.db import query_service as qs_mod  # noqa: E402
from app.services.db.collation_heal import (  # noqa: E402
    heal_join_collations,
    is_collation_error,
)
from app.services.db.query_service import QueryService  # noqa: E402

JOIN_SQL = (
    "SELECT p.name, SUM(s.amount) FROM sales s "
    "JOIN products p ON s.product_id = p.product_id "
    "WHERE s.d_date >= '2026-09-01' GROUP BY p.name"
)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ── Collation detection ──────────────────────────────────────────────────

def test_collation_error_detection():
    assert is_collation_error(RuntimeError(
        "Illegal mix of collations (utf8mb4_general_ci,IMPLICIT) and "
        "(utf8mb4_0900_ai_ci,IMPLICIT) for operation '='"))
    assert is_collation_error(RuntimeError("error 1267 on join"))
    assert is_collation_error(RuntimeError("utf8mb4 collation mismatch"))
    assert not is_collation_error(RuntimeError("syntax error near FROM"))
    assert not is_collation_error(RuntimeError("connection refused"))


# ── Collation healing ────────────────────────────────────────────────────

def test_heal_rewrites_cross_table_join():
    healed = heal_join_collations(JOIN_SQL, "mysql")
    assert healed is not None
    up = healed.upper()
    assert "CAST(S.PRODUCT_ID AS CHAR CHARACTER SET UTF8MB4)" in up
    assert "CAST(P.PRODUCT_ID AS CHAR CHARACTER SET UTF8MB4)" in up
    # the WHERE clause and GROUP BY survive untouched (sqlglot lowercases
    # identifiers, so compare case-insensitively)
    low = healed.lower()
    assert "s.d_date >= '2026-09-01'" in low
    assert "group by" in low


def test_heal_rewrites_all_joins():
    sql = "SELECT x.a FROM t x JOIN u y ON x.k = y.k JOIN v z ON y.k2 = z.k2"
    healed = heal_join_collations(sql, "mysql")
    assert healed is not None
    # 2 join predicates × both sides rewritten = 4 CASTs
    assert healed.upper().count("CHARACTER SET UTF8MB4") == 4


def test_heal_skips_same_table_and_literals():
    sql = ("SELECT * FROM orders o WHERE o.status = 'open' "
           "AND o.qty = o.limit_qty")
    # same-table comparison + literal → nothing to heal
    assert heal_join_collations(sql, "mysql") is None


def test_heal_unsupported_dialect_returns_none():
    assert heal_join_collations(JOIN_SQL, "postgres") is None
    assert heal_join_collations(JOIN_SQL, "sqlite") is None


def test_heal_unparseable_returns_none():
    assert heal_join_collations("SELECT FROM WHERE", "mysql") is None


def test_heal_reparseable():
    import sqlglot

    healed = heal_join_collations(JOIN_SQL, "mysql")
    assert sqlglot.parse_one(healed, dialect="mysql") is not None


# ── Timeout policy kill-switch ───────────────────────────────────────────

def _effective_timeout(timeout_s: int, disabled: bool) -> int:
    """Mirror of query_service's timeout clamp (regression pinning)."""
    if disabled:
        return 0
    return max(1, min(int(timeout_s), int(settings.MAX_QUERY_TIMEOUT_S)))


def test_timeout_disabled_yields_zero():
    assert _effective_timeout(10, disabled=True) == 0
    assert _effective_timeout(3600, disabled=True) == 0


def test_timeout_enabled_clamps_to_max():
    assert _effective_timeout(5, disabled=False) == 5
    assert _effective_timeout(3600, disabled=False) == settings.MAX_QUERY_TIMEOUT_S


def test_config_defaults_match_intent():
    assert settings.QUERY_TIMEOUT_DISABLED is True
    assert settings.COLLATION_HEAL_ENABLED is True
    assert settings.COLLATION_TARGET_CHARSET == "utf8mb4"


# ── Heal-and-retry integration at the execution boundary ─────────────────

class _FakeConnector:
    """Raises the collation error on the first execute, returns rows on the
    healed retry. Records every statement it was asked to run."""

    def __init__(self):
        self.calls: list[str] = []

    def execute(self, sql, max_rows=100, timeout_s=10):
        self.calls.append(sql)
        if len(self.calls) == 1:
            raise RuntimeError(
                "Illegal mix of collations (utf8mb4_general_ci,IMPLICIT) and "
                "(utf8mb4_0900_ai_ci,IMPLICIT) for operation '='"
            )
        return [{"product": "P1", "amount": 100}, {"product": "P2", "amount": 50}]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_query_service_heals_and_retries(db, monkeypatch):
    from types import SimpleNamespace

    import app.services.db.execution_gate as _gate

    kb = SimpleNamespace(
        id="kb-heal", name="w", db_type="mysql", source_kind="database",
    )
    svc = QueryService(db)
    conn = _FakeConnector()

    monkeypatch.setattr(qs_mod, "get_connector", lambda kb: conn)
    monkeypatch.setattr(QueryService, "_load_kb", lambda self, kb_id: kb)
    # unit test: bypass the real gate (it queries the live KnowledgeBase row)
    monkeypatch.setattr(_gate, "enforce", lambda *a, **kw: {"ok": True})

    result = svc.execute("kb-heal", JOIN_SQL, max_rows=100, timeout_s=10)
    assert result["rows"][0]["product"] == "P1"
    assert result["validation"]["collation_healed"] is True
    # two statements seen: the original + the healed retry
    assert len(conn.calls) == 2
    assert conn.calls[0] == JOIN_SQL
    assert "CHARACTER SET UTF8MB4" in conn.calls[1].upper()


def test_query_service_reraises_non_collation_errors(db, monkeypatch):
    from types import SimpleNamespace

    import app.services.db.execution_gate as _gate

    svc = QueryService(db)

    class _Boom:
        def execute(self, sql, max_rows=100, timeout_s=10):
            raise RuntimeError("connection refused")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    kb = SimpleNamespace(
        id="kb-heal2", name="w", db_type="mysql", source_kind="database",
    )
    monkeypatch.setattr(qs_mod, "get_connector", lambda kb: _Boom())
    monkeypatch.setattr(QueryService, "_load_kb", lambda self, kb_id: kb)
    monkeypatch.setattr(_gate, "enforce", lambda *a, **kw: {"ok": True})

    with pytest.raises(RuntimeError, match="connection refused"):
        svc.execute("kb-heal2", JOIN_SQL, max_rows=100, timeout_s=10)


# ── Timeout policy round 2: server-side limits zeroed when disabled ──────


class _FakeMySQLConn:
    """Records exec_driver_sql statements; execute() is configurable."""

    def __init__(self, fail_times=0):
        self.stmts = []
        self._fail_times = fail_times

    def exec_driver_sql(self, s):
        self.stmts.append(s)

    def execution_options(self, **kw):
        return self

    # engine.connect() is used as a context manager in execute()
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, params=None):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError(
                "(3024, 'Query execution was interrupted, maximum statement "
                "execution time exceeded')"
            )

        class _R:
            def returns_rows(self):
                return True

            def keys(self):
                return ["a"]

            def fetchmany(self, n):
                return [(1,)]

            def close(self):
                pass

        return _R()


class _FakeEngine:
    """Returns ONE connection object per connect() (pooled-conn simulation)."""

    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


def _mysql_connector(db_type="mysql", conn=None):
    from types import SimpleNamespace

    from app.services.db.mysql import MySQLConnector

    kb = SimpleNamespace(
        id="kb-timeout", name="w", db_type=db_type, source_kind="database",
        username="u", password="p", host="h", port=3306, database_name="d",
    )
    c = MySQLConnector(kb)
    c._engine = _FakeEngine(conn or _FakeMySQLConn())
    return c


def test_mysql_disabled_policy_zeroes_session_limit():
    """timeout_s=0 must SET SESSION MAX_EXECUTION_TIME = 0 — skipping the SET
    let the MySQL server's GLOBAL max_execution_time inherit into the session
    and kill heavy aggregations with error 3024 despite
    QUERY_TIMEOUT_DISABLED=True (live: June-August customer comparison)."""
    conn = _FakeMySQLConn()
    c = _mysql_connector(conn=conn)
    c.execute("SELECT 1", max_rows=10, timeout_s=0)
    assert "SET SESSION MAX_EXECUTION_TIME = 0" in conn.stmts
    # and no per-statement hint prepended
    assert not any("MAX_EXECUTION_TIME(" in s for s in conn.stmts)


def test_mysql_mariadb_disabled_policy_zeroes_max_statement_time():
    conn = _FakeMySQLConn()
    c = _mysql_connector(db_type="mariadb", conn=conn)
    c.execute("SELECT 1", max_rows=10, timeout_s=0)
    assert "SET SESSION max_statement_time = 0" in conn.stmts


def test_mysql_enabled_policy_still_sets_limit():
    conn = _FakeMySQLConn()
    c = _mysql_connector(conn=conn)
    c.execute("SELECT 1", max_rows=10, timeout_s=5)
    assert "SET SESSION MAX_EXECUTION_TIME = 5000" in conn.stmts
    # the per-statement hint rides on the executed SQL
    assert c.last_executed_sql.startswith("/*+ MAX_EXECUTION_TIME(5000) */")


def test_mysql_3024_self_heal_retries_once():
    """First attempt dies on the server's 3024; the self-heal retries once on
    a fresh checkout with the session limit explicitly zeroed."""
    conn = _FakeMySQLConn(fail_times=1)
    c = _mysql_connector(conn=conn)
    rows = c.execute("SELECT 1", max_rows=10, timeout_s=0)
    assert rows == [{"a": 1}]
    # the heal ran: a second SET SESSION MAX_EXECUTION_TIME = 0 was issued
    # before the retried statement
    zeros = [s for s in conn.stmts if s == "SET SESSION MAX_EXECUTION_TIME = 0"]
    assert len(zeros) >= 2


def test_mysql_no_retry_when_timeout_policy_configured():
    """timeout_s > 0 means the timeout is INTENTIONAL — 3024 must propagate."""
    conn = _FakeMySQLConn(fail_times=99)
    c = _mysql_connector(conn=conn)
    with pytest.raises(RuntimeError, match="3024"):
        c.execute("SELECT 1", max_rows=10, timeout_s=5)


def test_postgres_disabled_policy_zeroes_statement_timeout():
    from types import SimpleNamespace

    from app.services.db.postgres import PostgresConnector

    executed = []

    class _PgConn:
        def exec_driver_sql(self, s):
            executed.append(s)

        def execute(self, q, params=None):
            executed.append(str(q))

            class _R:
                def returns_rows(self):
                    return True

                def keys(self):
                    return ["a"]

                def fetchmany(self, n):
                    return [(1,)]

                def close(self):
                    pass

            return _R()

        def execution_options(self, **kw):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _PgEngine:
        def connect(self):
            return _PgConn()

    kb = SimpleNamespace(id="kb", name="w", db_type="postgres", source_kind="database")
    c = PostgresConnector(kb)
    c._engine = _PgEngine()
    c.execute("SELECT 1", max_rows=10, timeout_s=0)
    assert "SET LOCAL statement_timeout = 0" in executed
