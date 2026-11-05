"""The legacy entry point and the grounded path are ONE implementation.

The gap this pins: ``nl2sql.ask()`` used to be a second pipeline — its own
adapter factory, its own validator, its own policy check, its own execution call
— so the same question could be answered two different ways depending on which
entry point an agent reached, and the legacy one skipped source readiness, the
execution boundary, the grounded plan and the canonical result set.

``ask()`` is now a compatibility ADAPTER over ``NLAnswerService.answer()``. These
tests hold that:

* the two entry points produce the same source ids, plan fingerprint, SQL
  fingerprint, canonical result set, validation payload, disclosure and
  unsupported slots (parity);
* a legacy caller cannot execute a nonexistent table or an unauthorized source,
  and cannot execute anything without passing the boundary (regression);
* no source, schema, table or dialect is baked in, and an unidentifiable source
  is a refusal — never "the first database" and never "every source" (invariants).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.agent_data_binding import AgentDataBinding
from app.models.data_snapshot import DataSnapshot  # noqa: F401
from app.models.knowledge_base import KnowledgeBase
from app.services.db.contracts import CanonicalResultSet, GroundedPlan


# ── a real source, and the canonical path with only the LLM stubbed ────────


@pytest.fixture
def env(monkeypatch):
    """(db, source_id, sqlite path) with every infra-dependent stage off."""
    for flag, value in (
        ("KG_FRESHNESS_SHORTCIRCUIT_ENABLED", False),
        ("SCHEMA_LINKING_ENABLED", False),
        ("SEMANTIC_CATALOG_ENABLED", False),
        ("SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False),
        ("SCHEMA_VALIDATOR_DID_YOU_MEAN_ENABLED", False),
        ("NL2SQL_SHADOW_MODE_ENABLED", False),
        ("NL2SQL_PLAN_MEMORY_ENABLED", False),
        ("NL2SQL_DB_ERROR_REPAIR_MAX", 0),
    ):
        if hasattr(settings, flag):
            monkeypatch.setattr(settings, flag, value)

    from app.services.db import query_router

    monkeypatch.setattr(
        query_router, "resolve_static_route", lambda *a, **k: None, raising=False
    )

    src = tempfile.NamedTemporaryFile(suffix=".src.db", delete=False)
    src.close()
    con = sqlite3.connect(src.name)
    con.execute("CREATE TABLE customers (id INTEGER, name TEXT, region TEXT)")
    con.execute("INSERT INTO customers VALUES (1,'Alice','EU'),(2,'Bob','US')")
    con.commit()
    con.close()

    appdb = tempfile.NamedTemporaryFile(suffix=".app.db", delete=False)
    appdb.close()
    eng = create_engine(f"sqlite:///{appdb.name}")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    add_source(db, src.name, kb_id="kb-src-1", name="acme_warehouse")

    yield db, "kb-src-1", src.name

    db.close()
    eng.dispose()
    for path in (src.name, appdb.name):
        os.unlink(path)


def add_source(
    db,
    path,
    *,
    kb_id,
    name="source",
    ready=True,
    host=None,
    port=None,
    database=None,
    db_type="sqlite",
):
    kb = KnowledgeBase(
        id=kb_id,
        name=name,
        type="database",
        source_kind="database",
        db_type=db_type,
        file_url=path,
        database_name=database if database is not None else path,
        host=host,
        port=port,
        catalog_status="ready" if ready else "pending",
        nl2sql_state="nl2sql_ready" if ready else "",
    )
    db.add(kb)
    db.commit()
    return kb


def llm(sql, monkeypatch):
    async def _text_to_sql(self, *args, **kwargs):
        return sql

    monkeypatch.setattr(
        "app.services.db.nl_answer_service.NLAnswerService._text_to_sql", _text_to_sql
    )
    return sql


def record_connectors(monkeypatch):
    """Record every connector the platform opens — a real source must be opened once."""
    opened: list[str] = []
    from app.services.db import query_service as qs

    real = qs.get_connector

    def _factory(kb):
        opened.append(str(getattr(kb, "id", "")))
        return real(kb)

    monkeypatch.setattr(qs, "get_connector", _factory)
    return opened


QUESTION = "list the customers"


# ── 5. parity between the two entry points ────────────────────────────────


def test_both_entry_points_produce_the_same_answer_contracts(env, monkeypatch):
    """Same question, both entry points → identical grounded contracts."""
    from app.services.db.nl_answer_service import NLAnswerService
    from app.services.nl2sql import ask

    db, kb_id, _ = env
    llm("SELECT id, name FROM customers", monkeypatch)

    legacy = ask(QUESTION, kb_id=kb_id, db=db)
    canonical = asyncio.run(
        NLAnswerService(db).answer(kb_id, QUESTION, skip_narrate=True)
    )

    assert legacy.success and canonical["success"]

    # selected source ids
    assert legacy.plan["source_ids"] == canonical["plan"]["source_ids"] == [kb_id]
    assert legacy.result_set["source_ids"] == canonical["result_set"]["source_ids"]

    # grounded plan fingerprint — the same decision, not a similar one
    assert (
        GroundedPlan.from_dict(legacy.plan).fingerprint()
        == GroundedPlan.from_dict(canonical["plan"]).fingerprint()
    )

    # SQL fingerprint (authored and executed)
    for key in ("sql_fingerprint", "executed_sql_fingerprint", "tables"):
        assert legacy.provenance.get(key) == canonical["provenance"].get(key)

    # canonical result set
    assert (
        CanonicalResultSet.from_dict(legacy.result_set).fingerprint()
        == CanonicalResultSet.from_dict(canonical["result_set"]).fingerprint()
    )

    # validation payload — what the boundary verified before dispatch
    assert legacy.result_set["execution_validation"] == canonical["result_set"]["execution_validation"]
    assert legacy.result_set["execution_validation"]["ok"] is True
    assert (
        legacy.result_set["execution_validation"]["authored_sql"]
        == "SELECT id, name FROM customers"
    )

    # disclosure + unsupported slots (both empty here — and both equally empty)
    assert legacy.unsupported_slots == canonical.get("unsupported_slots", [])
    assert legacy.disclosure == canonical.get("disclosure", "")


def test_a_legacy_caller_keeps_every_grounded_contract(env, monkeypatch):
    """The adapter must carry the plan, result set, slots, disclosure and
    provenance through — dropping them hides the sentence that says part of the
    question could not be answered."""
    from app.services.nl2sql import NL2SQLResult, ask
    from app.services.nl2sql import compat

    db, kb_id, _ = env
    canonical = {
        "success": True,
        "answer": "Revenue was 120; margin could not be computed.",
        "rows": [{"revenue": 120}],
        "columns": ["revenue"],
        "sql": "SELECT SUM(amount) AS revenue FROM customers",
        "source_id": kb_id,
        "source_name": "acme_warehouse",
        "provenance": {"sql_fingerprint": "abc123", "tables": ["customers"]},
        "plan": {"source_ids": [kb_id], "tables": ["customers"], "dialect": "sqlite"},
        "result_set": {
            "source_ids": [kb_id],
            "rows": [{"revenue": 120}],
            "complete": False,
            "disclosure": "Margin is not available in this source.",
        },
        "unsupported_slots": [
            {"slot": "margin", "label": "margin", "reason": "unmapped_metric"}
        ],
        "disclosure": "Margin is not available in this source.",
    }
    monkeypatch.setattr(compat, "run_canonical", lambda *a, **kw: canonical)

    r = ask(QUESTION, kb_id=kb_id, db=db)

    assert isinstance(r, NL2SQLResult)
    assert r.plan == canonical["plan"]
    assert r.result_set == canonical["result_set"]
    assert r.unsupported_slots == canonical["unsupported_slots"]
    assert r.disclosure == canonical["disclosure"]
    assert r.provenance == canonical["provenance"]
    assert r.source_name == "acme_warehouse"

    # And the legacy response shape a strict caller declares still holds.
    from app.services.db.nl_answer_service import legacy_result_view

    legacy_only = legacy_result_view(dict(canonical))
    assert "plan" not in legacy_only and "result_set" not in legacy_only


# ── 6. a legacy caller cannot bypass the boundary ─────────────────────────


def test_legacy_caller_cannot_execute_a_nonexistent_table(env, monkeypatch):
    from app.services.nl2sql import ask

    db, kb_id, _ = env
    opened = record_connectors(monkeypatch)
    llm("SELECT id, fee FROM no_such_table", monkeypatch)

    r = ask(QUESTION, kb_id=kb_id, db=db)

    assert not r.success
    assert r.rows == []
    assert r.error_kind == "execution_validation_failed"
    assert "no_such_table" in r.error
    # Nothing reached the driver: the statement was refused at the boundary.
    assert opened == []
    # The refusal is a structured object, not a string.
    assert r.validation is not None and not r.validation.is_valid
    assert r.result_set["execution_validation"]["ok"] is False
    assert r.result_set["execution_validation"]["authored_sql"] == "SELECT id, fee FROM no_such_table"
    assert r.result_set["execution_validation"]["executed_sql"] == ""
    assert r.disclosure, "a refusal must say so in business language"
    assert r.plan is not None, "even a refusal carries the plan it decided against"


def test_legacy_caller_cannot_execute_an_unauthorized_source(env, monkeypatch):
    from app.services.nl2sql import ask

    db, kb_id, _ = env
    db.add(
        AgentDataBinding(
            id="b-legacy-1",
            agent_app_id="a-legacy-1",
            datasource_id=kb_id,
            allowed_tables=["orders_only"],
            access_mode="read_only",
        )
    )
    db.commit()
    llm("SELECT id, name FROM customers", monkeypatch)

    r = ask(QUESTION, binding_id="b-legacy-1", db=db)

    assert not r.success
    assert r.rows == []
    assert "customers" in (r.error + r.disclosure)


def test_ask_never_builds_a_legacy_adapter(env, monkeypatch):
    """There is no second execution path: the adapter factory is off the path."""
    import app.services.nl2sql as nl2sql

    db, kb_id, _ = env
    llm("SELECT id, name FROM customers", monkeypatch)

    def _forbidden(*a, **kw):  # pragma: no cover - must not run
        raise AssertionError("ask() must not build a datasource adapter")

    monkeypatch.setattr(nl2sql, "_build_adapter", _forbidden, raising=False)

    r = nl2sql.ask(QUESTION, kb_id=kb_id, db=db)
    assert r.success


def test_one_source_one_connection(env, monkeypatch):
    """A question is answered from the named source — never from all of them."""
    from app.services.nl2sql import ask

    db, kb_id, path = env
    add_source(db, path, kb_id="kb-src-2", name="another_warehouse")
    opened = record_connectors(monkeypatch)
    llm("SELECT id, name FROM customers", monkeypatch)

    r = ask(QUESTION, kb_id=kb_id, db=db)

    assert r.success
    assert set(opened) == {kb_id}, f"opened {opened}"


# ── 7. dynamic sources: no baked-in database, dialect or fallback ─────────


def test_an_unidentifiable_source_is_refused_not_defaulted(env, monkeypatch):
    """Several ready sources exist; a call that names none gets a refusal.

    "Use the first database" is how a question about one company's sales gets
    answered from another company's warehouse.
    """
    from app.services.nl2sql import ask

    db, kb_id, path = env
    add_source(db, path, kb_id="kb-src-2", name="second_warehouse")
    opened = record_connectors(monkeypatch)
    llm("SELECT id, name FROM customers", monkeypatch)

    r = ask(QUESTION, binding_id="wf:unknown:deadbeef", db=db)

    assert not r.success
    assert r.error_kind == "source_not_resolved"
    assert r.plan is None and r.result_set is None
    assert opened == [], "nothing may be executed when no source was identified"
    assert "data source" in r.error


def test_an_ambiguous_connection_is_refused_rather_than_picked(env, monkeypatch):
    """Two sources with the same connection identity → refuse, list both."""
    from app.services.nl2sql import ask, compat

    db, kb_id, path = env
    add_source(db, path, kb_id="kb-src-2", name="second_warehouse")

    resolution = compat.resolve_source(
        db,
        datasource_config={"dialect": "sqlite", "path": path, "database": path},
    )
    assert not resolution.ok
    assert resolution.kb_id == ""


def test_the_dialect_follows_the_source_not_a_default(env, monkeypatch):
    """Both sources are validated in THEIR OWN dialect, read from the row.

    The platform must not assume one database type: a MariaDB source is validated
    as the MySQL family and a SQLite source as SQLite, and the statement the
    driver receives is the one that was authored — never a transliteration
    against a default dialect.
    """
    from app.services.db import nl_answer_service as nls
    from app.services.db import query_service as qs
    from app.services.db import execution_gate as gate
    from app.services.nl2sql import ask

    db, kb_id, _ = env
    add_source(
        db, "/tmp/not-a-real-file.db", kb_id="kb-maria", name="maria_warehouse",
        db_type="mariadb", database="kunden_db", host="maria-host", port=3306,
    )

    async def _schema(self, kb_id, question):
        return (
            {
                "source": {"id": kb_id, "name": "maria_warehouse", "db_type": "mariadb"},
                "tables": [{
                    "table": "kunden",
                    "columns": [{"name": "kundennr", "type": "varchar", "nullable": True, "pk": True}],
                }],
            },
            "TABLE kunden ( kundennr varchar )",
        )

    monkeypatch.setattr(nls.NLAnswerService, "_build_m_schema_fallback", _schema)
    monkeypatch.setattr(gate, "_known_tables", lambda db, kb_id: {"kunden", "customers"})
    monkeypatch.setattr(
        gate, "_described_columns",
        lambda db, kb_id, table: {"kunden": ["kundennr"], "customers": ["id", "name", "region"]}.get(
            table.split(".")[-1].lower()
        ),
    )

    seen: dict = {}

    class _Conn:
        last_executed_sql = ""

        def execute(self, sql, max_rows=10, timeout_s=5, params=None):
            # Production passes params (parameterized SQL); the fixture ignores
            # them — this test only cares that the dialect-faithful statement is
            # handed to the driver unchanged.
            seen["sql"] = sql
            return [{"n": 1}]

    @contextlib.contextmanager
    def _cm(kb):
        yield _Conn()

    monkeypatch.setattr(qs, "get_connector", lambda kb: _cm(kb))
    llm("SELECT kundennr FROM kunden", monkeypatch)

    maria = ask("how many customers", kb_id="kb-maria", db=db)
    assert maria.success, maria.error
    maria_payload = maria.result_set["execution_validation"]
    assert maria_payload["dialect"] == "mysql", "mariadb profiles to the MySQL family"
    assert maria_payload["detail"]["db_type"] == "mariadb", "read from the source row"
    assert seen["sql"] == "SELECT kundennr FROM kunden", "no dialect-specific rewrite on MySQL"

    sqlite = ask(QUESTION, kb_id=kb_id, db=db)
    assert sqlite.success, sqlite.error
    sqlite_payload = sqlite.result_set["execution_validation"]
    assert sqlite_payload["dialect"] == "sqlite"
    assert sqlite_payload["detail"]["db_type"] == "sqlite"


def test_source_and_table_names_are_read_from_the_data(env, monkeypatch):
    """An arbitrary customer's names answer without any name-specific code."""
    from app.services.nl2sql import ask

    db, _, _ = env
    src = tempfile.NamedTemporaryFile(suffix=".erp.db", delete=False)
    src.close()
    con = sqlite3.connect(src.name)
    con.execute("CREATE TABLE t_sd_0231_v_9 (kunnr TEXT, netwr REAL)")
    con.execute("INSERT INTO t_sd_0231_v_9 VALUES ('0001', 12.5)")
    con.commit()
    con.close()

    add_source(db, src.name, kb_id="kb-erp-x", name="erp_prod_2026")
    llm("SELECT kunnr, netwr FROM t_sd_0231_v_9", monkeypatch)

    r = ask("revenue by customer", kb_id="kb-erp-x", db=db)
    assert r.success, r.error
    assert r.rows == [{"kunnr": "0001", "netwr": 12.5}]
    assert r.source_name == "erp_prod_2026"
    assert "t_sd_0231_v_9" in (r.provenance.get("tables") or [])
    os.unlink(src.name)


def test_a_source_that_is_not_ready_is_refused(env, monkeypatch):
    from app.services.nl2sql import ask

    db, _, path = env
    add_source(db, path, kb_id="kb-not-ready", name="indexing_now", ready=False)
    opened = record_connectors(monkeypatch)
    llm("SELECT id FROM customers", monkeypatch)

    r = ask(QUESTION, kb_id="kb-not-ready", db=db)

    assert not r.success
    assert r.error_kind == "source_not_ready"
    assert r.disclosure
    assert opened == []
