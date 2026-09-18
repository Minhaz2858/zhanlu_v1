"""Session guards must never run under `stream_results`.

Live regression this pins (2026-09-18): setting `stream_results=True` on the
CONNECTION made Postgres wrap every statement in
`DECLARE <name> CURSOR WITHOUT HOLD FOR ...`, and
`DECLARE ... FOR SET LOCAL statement_timeout = 10000` is a syntax error — so
every Postgres query failed. The sqlite-backed unit suite could not see it
(only psycopg2 builds server-side cursors that way), and the MySQL path hid it
because pymysql swaps in SSCursor at the DBAPI level.

The invariant, dialect-agnostic and testable: any statement executed while
`stream_results` is active must be the DATA statement — never a SET.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.db.mysql import MySQLConnector
from app.services.db.postgres import PostgresConnector


class _FakeResult:
    returns_rows = True
    rowcount = 0

    def keys(self):
        return ["a"]

    def fetchmany(self, n):
        return [(1,)]

    def close(self):
        pass


class _FakeConnection:
    """Records every statement together with the execution options in force."""

    def __init__(self):
        self.log: list[tuple[str, str, dict]] = []
        self._opts: dict = {}

    def execution_options(self, **kw):
        self._opts.update(kw)
        return self

    def exec_driver_sql(self, stmt):
        self.log.append(("driver_sql", str(stmt), dict(self._opts)))
        return _FakeResult()

    def execute(self, stmt, params=None):
        self.log.append(("execute", str(stmt), dict(self._opts)))
        return _FakeResult()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self):
        self.conn = _FakeConnection()

    def connect(self):
        return self.conn


def _run(connector, sql="SELECT 1", timeout_s=10):
    engine = _FakeEngine()
    connector._engine = engine
    connector.execute(sql, max_rows=10, timeout_s=timeout_s)
    return engine.conn.log


def test_postgres_never_sets_a_session_variable_while_streaming():
    log = _run(PostgresConnector(SimpleNamespace(db_type="postgresql")))
    streamed = [stmt for _kind, stmt, opts in log if opts.get("stream_results")]
    assert streamed, "the data statement should be streamed"
    assert not any(stmt.lstrip().upper().startswith("SET") for stmt in streamed), streamed
    assert any(stmt.upper().startswith("SET DEFAULT_TRANSACTION_READ_ONLY")
               for _k, stmt, _o in log)


def test_postgres_guards_run_before_the_data_statement():
    log = _run(PostgresConnector(SimpleNamespace(db_type="postgresql")))
    kinds = [stmt for _k, stmt, _o in log]
    assert any("statement_timeout" in s for s in kinds)
    assert kinds.index([s for s in kinds if "statement_timeout" in s][0]) < len(kinds) - 1


def test_postgres_streams_only_the_final_statement():
    log = _run(PostgresConnector(SimpleNamespace(db_type="postgresql")))
    assert len([1 for _k, _s, opts in log if opts.get("stream_results")]) == 1


def test_mysql_read_only_and_timeout_guards_are_issued():
    log = _run(MySQLConnector(SimpleNamespace(db_type="mysql")))
    stmts = [s for _k, s, _o in log]
    assert "SET SESSION TRANSACTION READ ONLY" in stmts
    assert any(s.startswith("SET SESSION MAX_EXECUTION_TIME") for s in stmts)


def test_mariadb_uses_its_own_timeout_variable():
    """MariaDB ignores MAX_EXECUTION_TIME — it needs max_statement_time (seconds)."""
    log = _run(MySQLConnector(SimpleNamespace(db_type="mariadb")), timeout_s=7)
    stmts = [s for _k, s, _o in log]
    assert "SET SESSION max_statement_time = 7" in stmts
    # The hint comment is still prefixed to the data SQL, but the SESSION variable
    # for MariaDB must be max_statement_time, not MAX_EXECUTION_TIME.
    assert not any(s.startswith("SET SESSION MAX_EXECUTION_TIME") for s in stmts)


def test_mysql_never_sets_a_session_variable_while_streaming():
    log = _run(MySQLConnector(SimpleNamespace(db_type="mysql")))
    streamed = [stmt for _k, stmt, opts in log if opts.get("stream_results")]
    assert len(streamed) == 1, streamed
    assert "SELECT 1" in streamed[0]                     # the data statement
    assert not any(s.lstrip().upper().startswith("SET") for s in streamed)


def test_a_zero_timeout_skips_the_timeout_guard_but_keeps_read_only():
    log = _run(MySQLConnector(SimpleNamespace(db_type="mysql")), timeout_s=0)
    stmts = [s for _k, s, _o in log]
    assert "SET SESSION TRANSACTION READ ONLY" in stmts
    assert not any("MAX_EXECUTION_TIME" in s for s in stmts)


def test_a_non_select_statement_is_never_wrapped_in_a_cursor():
    """DECLARE ... FOR <non-SELECT> is a syntax error on Postgres."""
    log = _run(MySQLConnector(SimpleNamespace(db_type="mysql")), sql="SHOW TABLES")
    assert not any(opts.get("stream_results") for _k, _s, opts in log)
    log = _run(PostgresConnector(SimpleNamespace(db_type="postgresql")), sql="SHOW TABLES")
    assert not any(opts.get("stream_results") for _k, _s, opts in log)


def test_a_with_query_is_streamed():
    log = _run(PostgresConnector(SimpleNamespace(db_type="postgresql")),
               sql="WITH x AS (SELECT 1) SELECT * FROM x")
    assert len([1 for _k, _s, opts in log if opts.get("stream_results")]) == 1
