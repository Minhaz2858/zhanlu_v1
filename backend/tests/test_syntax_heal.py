"""Syntax self-heal tests (ER 1064 class) — reserved-word aliases.

Measured live: the NL2SQL model wrote ``COUNT(*) AS lines`` — ``lines`` is a
MySQL 8 reserved word — so the weekly-compare dataset died with a 1064
syntax error, the deliverable phase-lock then refused to build the report on
incomplete data, and the turn ended with a promise instead of a report.

The heal is driven by the DATABASE'S OWN error message (the "near '…'"
fragment) plus alias-position analysis — no table names, no domain
vocabulary, works on any MySQL-family warehouse.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def sh():
    from app.services.db import syntax_heal

    return syntax_heal


LIVE_ERROR = (
    "(pymysql.err.ProgrammingError) (1064, \"You have an error in your SQL "
    "syntax; check the manual that corresponds to your MySQL server version "
    "for the right syntax to use near 'lines FROM erp_v_sale_orderentry "
    "WHERE PLANDATE' at line 1\")"
)


# ── detection ───────────────────────────────────────────────────────────────


def test_detects_mysql_1064(sh):
    assert sh.is_syntax_error(Exception(LIVE_ERROR))
    assert sh.is_syntax_error(Exception("1064: syntax error near 'x'"))


def test_ignores_other_errors(sh):
    assert not sh.is_syntax_error(Exception("Unknown column 'x' (1054)"))
    assert not sh.is_syntax_error(Exception("Illegal mix of collations"))
    assert not sh.is_syntax_error(Exception("connection refused"))


# ── healing ─────────────────────────────────────────────────────────────────


def test_heals_reserved_alias_from_error_token(sh):
    sql = (
        "SELECT CASE WHEN PLANDATE >= '2026-10-12' THEN 'this_week' ELSE "
        "'prev_week' END AS wk, COUNT(*) AS lines FROM erp_v_sale_orderentry "
        "WHERE PLANDATE >= '2026-10-12'"
    )
    healed = sh.heal_syntax(sql, Exception(LIVE_ERROR), "mysql")
    assert healed is not None
    assert "AS `lines`" in healed
    assert "AS wk" in healed  # non-reserved alias untouched


def test_heals_all_reserved_aliases_in_one_pass(sh):
    # One retry must fix EVERY reserved alias, not just the first error.
    sql = (
        "SELECT COUNT(*) AS lines, SUM(x) AS `ok`, ROW_NUMBER() OVER () AS "
        "rank FROM t"
    )
    healed = sh.heal_syntax(sql, Exception(LIVE_ERROR), "mysql")
    assert "AS `lines`" in healed
    assert "AS `rank`" in healed
    assert "AS `ok`" in healed  # already quoted — untouched


def test_no_heal_when_no_reserved_alias(sh):
    sql = "SELECT COUNT(*) AS cnt FROM t WHERE a = 1"
    assert sh.heal_syntax(sql, Exception(LIVE_ERROR), "mysql") is None


def test_no_heal_for_other_dialects(sh):
    sql = "SELECT COUNT(*) AS lines FROM t"
    assert sh.heal_syntax(sql, Exception(LIVE_ERROR), "postgres") is None
    assert sh.heal_syntax(sql, Exception(LIVE_ERROR), "sqlite") is None


def test_no_heal_when_error_is_not_syntax(sh):
    sql = "SELECT COUNT(*) AS lines FROM t"
    assert sh.heal_syntax(sql, Exception("Unknown column 'x'"), "mysql") is None


def test_idempotent_on_already_healed_sql(sh):
    sql = "SELECT COUNT(*) AS `lines` FROM t"
    healed = sh.heal_syntax(sql, Exception(LIVE_ERROR), "mysql")
    assert healed is None  # nothing left to heal


# ── QueryService wiring ─────────────────────────────────────────────────────


def test_query_service_retries_with_healed_sql(db_with_mysql_kb, monkeypatch):
    """A 1064 on first execute must heal the alias and retry ONCE."""
    from app.services.db import query_service as qs_mod

    db, kb = db_with_mysql_kb
    calls = {"n": 0}

    class FakeConn:
        dialect = "mysql"
        last_executed_sql = ""

        def execute(self, sql, **kw):
            calls["n"] += 1
            calls["sql"] = sql
            if calls["n"] == 1:
                raise Exception(LIVE_ERROR)
            return [{"n": 1}]

    monkeypatch.setattr(
        qs_mod, "get_connector", lambda kb: _ctx(FakeConn())
    )

    svc = qs_mod.QueryService(db)
    out = svc.execute(kb.id, "SELECT COUNT(*) AS lines FROM erp_t")
    assert out["rows"] == [{"n": 1}]
    assert calls["n"] == 2
    assert "AS `lines`" in calls["sql"]
    assert out["validation"]["syntax_healed"] is True


def test_query_service_raises_when_unhealable(db_with_mysql_kb, monkeypatch):
    from app.services.db import query_service as qs_mod

    db, kb = db_with_mysql_kb
    calls = {"n": 0}

    class FakeConn:
        dialect = "mysql"
        last_executed_sql = ""

        def execute(self, sql, **kw):
            calls["n"] += 1
            raise Exception("syntax error near 'garbage garbage' (1064)")

    monkeypatch.setattr(
        qs_mod, "get_connector", lambda kb: _ctx(FakeConn())
    )

    svc = qs_mod.QueryService(db)
    with pytest.raises(Exception):
        svc.execute(kb.id, "SELECT * FROM erp_t")
    assert calls["n"] == 1  # no blind retry


class _ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


@pytest.fixture()
def db_with_mysql_kb():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.models.base import Base
    from app.models.knowledge_base import KnowledgeBase

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    session = Session()
    kb = KnowledgeBase(
        id="kb-syn", name="syn", db_type="mysql", org_id="default-org",
        source_kind="database",
    )
    session.add(kb)
    session.commit()
    yield session, kb
    session.close()
    eng.dispose()
