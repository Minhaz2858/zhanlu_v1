"""The legacy ``nl2sql.ask()`` entry point, end to end through the canonical path.

These tests used to drive the legacy pipeline's own seams (its adapter, its
validator, its ``DataSnapshotService.nl2sql`` call, its retry loop). ``ask()`` is
now a compatibility adapter over ``db/nl_answer_service``, so they were rewritten
at the seam the product actually uses:

    question → source resolution → readiness → retrieval/linking → SQL →
    QueryService.execute (the execution boundary) → grounded plan →
    canonical result set → disclosure

A real SQLite source is used, so the gate introspects real tables and the
connector really runs the statement. Only the LLM step is stubbed — it is the one
part that needs a model, and stubbing it is what makes the run deterministic.
"""

import os
import sqlite3
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.agent_data_binding import AgentDataBinding
from app.models.data_snapshot import DataSnapshot
from app.models.knowledge_base import KnowledgeBase


@pytest.fixture
def pipeline_db():
    """A file-backed SQLAlchemy DB with all required tables.

    File-backed, not in-memory: the answering path executes the statement on a
    worker thread (``asyncio.to_thread``), and an in-memory SQLite connection
    cannot be used from another thread. Production runs on a real server; this
    keeps the test honest about the same constraint.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".appdb", delete=False)
    tmp.close()
    eng = create_engine(f"sqlite:///{tmp.name}")
    Base.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng)
    s = Sess()
    yield s
    s.close()
    eng.dispose()
    os.unlink(tmp.name)


@pytest.fixture
def sqlite_ds_path():
    """A temporary SQLite database with real rows, and a real schema."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    con.execute(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, region TEXT)"
    )
    con.execute(
        "CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount REAL)"
    )
    con.execute("INSERT INTO customers VALUES (1,'Alice','EU'),(2,'Bob','US')")
    con.execute("INSERT INTO orders VALUES (1,1,100.0),(2,2,50.0)")
    con.commit()
    con.close()
    yield tmp.name
    os.unlink(tmp.name)


def make_source(db, sqlite_ds_path, *, kb_id="kb-sqlite-1", ready=True):
    """A KnowledgeBase the readiness gate will pass (or refuse, when not ready)."""
    kb = KnowledgeBase(
        id=kb_id,
        name="Test SQLite source",
        type="database",
        source_kind="database",
        db_type="sqlite",
        # Both spellings: the connector reads ``file_url``, the datasource
        # adapter (M-Schema) reads ``database_name`` for a file path.
        file_url=sqlite_ds_path,
        database_name=sqlite_ds_path,
        catalog_status="ready" if ready else "pending",
        nl2sql_state="nl2sql_ready" if ready else "",
    )
    db.add(kb)
    db.commit()
    return kb


@pytest.fixture
def offline(monkeypatch):
    """Turn off every optional pipeline stage that needs infrastructure.

    Readiness, linking, retrieval, the execution boundary and the grounded plan
    stay ON: they are what these tests are about. What is disabled is the
    catalog/vector machinery and the shadow logger, which need a built catalog
    and a live deployment.
    """
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

    def canned(sql: str):
        """Make the one LLM call in the pipeline return *sql*."""

        async def _text_to_sql(self, *args, **kwargs):
            return sql

        monkeypatch.setattr(
            "app.services.db.nl_answer_service.NLAnswerService._text_to_sql",
            _text_to_sql,
        )
        return sql

    return canned


# ── the legacy entry point answers, through the canonical path ────────────


def test_ask_answers_from_a_real_source_and_persists_a_snapshot(
    pipeline_db, sqlite_ds_path, offline
):
    from app.services.nl2sql import ask

    make_source(pipeline_db, sqlite_ds_path)
    canned = "SELECT id, name FROM customers"
    offline(canned)

    r = ask("show customers", kb_id="kb-sqlite-1", db=pipeline_db)

    assert r.success, r.error
    assert r.sql == canned
    assert r.data is not None and r.data.row_count == 2
    assert {row["name"] for row in r.rows} == {"Alice", "Bob"}
    assert r.answer, "a legacy caller still gets prose"
    # The grounded contracts travel with a legacy answer too.
    assert r.plan is not None and r.result_set is not None, "plan + result set"
    assert r.plan["source_ids"] == ["kb-sqlite-1"]
    assert r.result_set["source_ids"] == ["kb-sqlite-1"]
    assert r.provenance.get("sql_fingerprint")
    assert r.validation is not None and r.validation.is_valid

    snap = pipeline_db.query(DataSnapshot).first()
    assert snap is not None, "Expected a DataSnapshot to be persisted"
    assert snap.sql_query == canned
    assert snap.row_count == 2
    assert "Alice" in str(snap.result_data)


def test_ask_keeps_the_legacy_binding_allowlist_effective(
    pipeline_db, sqlite_ds_path, offline
):
    """A binding that allows only `customers` still cannot read `orders`."""
    from app.services.nl2sql import ask

    make_source(pipeline_db, sqlite_ds_path)
    pipeline_db.add(
        AgentDataBinding(
            id="b-allow-1",
            agent_app_id="a-allow-1",
            datasource_id="kb-sqlite-1",
            allowed_tables=["customers"],
            access_mode="read_only",
        )
    )
    pipeline_db.commit()
    offline("SELECT id, amount FROM orders")

    r = ask("show orders", binding_id="b-allow-1", db=pipeline_db)

    assert not r.success
    assert r.rows == []
    # The refusal is structured: it names the table and reports no figure.
    assert "orders" in (r.error + r.disclosure)
    assert r.error_kind or r.disclosure or r.error


# ── 2B-3 review requirement 3: the scope disclosure chain ──────────────────


def test_scope_disclosure_survives_gate_to_answer(
    pipeline_db, sqlite_ds_path, monkeypatch
):
    """The disclosure chain on REAL code, no scope machinery stubbed:

        scope evidence (catalog) → P-SV plan disclosure + execution-gate
        warning → NLAnswerService result → answer_text

    ``answer_text`` is the exact content every caller streams and persists
    (the FSM/ReAct persistence contract is pinned separately by
    test_fsm_persistence_stage1), so a disclosure present here is present in
    the streamed and persisted answer.

    The catalog knows ``customers`` ONLY as a confirmed regional partition
    and holds NO company-wide dataset; the question is company-wide. The
    result must carry the incompleteness at every stage.
    """
    import uuid as _uuid

    from app.models.knowledge_catalog import KBTableMeta
    from app.services.db import nl_answer_service as nls
    from app.services.db import query_router

    make_source(pipeline_db, sqlite_ds_path)
    pipeline_db.add(KBTableMeta(
        id=str(_uuid.uuid4()), kb_id="kb-sqlite-1", schema_name="",
        table_name="customers", table_type="TABLE",
        description_en="customer records",
        dataset_scope="partition:region", dataset_scope_confidence=0.6,
        dataset_scope_signals_json={"partition_value": "north"},
    ))
    pipeline_db.commit()

    # The `offline` disables, minus the linking flags — this test needs the
    # full Phase A (TOC → retrieval → facts) so the gate gets scope context.
    monkeypatch.setattr(settings, "KG_FRESHNESS_SHORTCIRCUIT_ENABLED", False)
    monkeypatch.setattr(settings, "SCHEMA_LINKING_ENABLED", True)
    monkeypatch.setattr(settings, "SEMANTIC_CATALOG_ENABLED", True)
    monkeypatch.setattr(settings, "SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False)
    monkeypatch.setattr(
        settings, "SCHEMA_VALIDATOR_DID_YOU_MEAN_ENABLED", False,
    )
    monkeypatch.setattr(settings, "NL2SQL_SHADOW_MODE_ENABLED", False)
    monkeypatch.setattr(settings, "NL2SQL_PLAN_MEMORY_ENABLED", False)
    monkeypatch.setattr(settings, "NL2SQL_DB_ERROR_REPAIR_MAX", 0)
    monkeypatch.setattr(
        settings, "SCHEMA_CLASSIFICATION_RETRIEVAL_ENABLED", True,
    )
    monkeypatch.setattr(
        settings, "SCHEMA_SCOPE_AUTHORIZATION_ENABLED", True,
    )
    monkeypatch.setattr(
        query_router, "resolve_static_route", lambda *a, **k: None,
        raising=False,
    )

    # Deterministic selection + SQL: the model picks the partition table.
    async def _fake_chat(messages, **kw):
        return '["customers"]'

    monkeypatch.setattr(nls, "_chat", _fake_chat)

    async def _text_to_sql(self, *args, **kwargs):
        return "SELECT id, name FROM customers"

    monkeypatch.setattr(
        "app.services.db.nl_answer_service.NLAnswerService._text_to_sql",
        _text_to_sql,
    )

    from app.services.nl2sql import ask

    r = ask("show me all customers across the whole company", kb_id="kb-sqlite-1", db=pipeline_db)

    assert r.success, r.error
    # Stage 2: the NLAnswerService RESULT carries the scope verdict.
    assert getattr(r, "scope_disclosure", None), "structured scope disclosure"
    assert "partition" in r.scope_disclosure
    # Stage 1 evidence: the execution gate RAN the scope check (warning, not
    # skip) — the verbatim state is on the result.
    assert getattr(r, "scope_check", None) == "pass"
    # Stage 3: the answer text (what synthesis receives / what is streamed
    # and persisted) contains the disclosure.
    assert r.answer and "partition" in r.answer, (
        "the disclosure must be inside the answer text itself"
    )
    assert "north" in r.answer or "could not be fully verified" in r.answer or \
        "may not represent" in r.answer
    # Three layers describe the same fact (retrieval sentence, P-SV plan
    # disclosure, gate warning) — the reader sees it once, stated cleanly.
    assert r.scope_disclosure.count("no company-wide dataset exists") == 1
    assert r.answer.count("no company-wide dataset exists") == 1


# ── review requirement B: degraded catalog must be visible ──────────────────


class TestScopeLimitationSentence:
    """The five gate states map to honest reader-facing behavior."""

    def test_unavailable_discloses_verification_not_possible(self):
        from app.services.db.nl_answer_service import (
            _scope_limitation_sentence,
        )

        s = _scope_limitation_sentence("unavailable", False)
        assert "could not be verified" in s
        # must not claim anything about the data itself
        assert "incomplete" not in s
        assert "may not represent" not in s

    def test_not_applicable_with_degraded_loader_discloses(self):
        from app.services.db.nl_answer_service import (
            _scope_limitation_sentence,
        )

        s = _scope_limitation_sentence("not-applicable", True)
        assert "could not be verified" in s

    def test_clean_not_applicable_is_silent(self):
        from app.services.db.nl_answer_service import (
            _scope_limitation_sentence,
        )

        assert _scope_limitation_sentence("not-applicable", False) == ""

    def test_pass_fail_not_in_scope_are_silent(self):
        from app.services.db.nl_answer_service import (
            _scope_limitation_sentence,
        )

        assert _scope_limitation_sentence("pass", False) == ""
        assert _scope_limitation_sentence("fail", False) == ""
        assert _scope_limitation_sentence("not-in-scope", False) == ""
        assert _scope_limitation_sentence(None, False) == ""


async def test_pipeline_unavailable_catalog_is_user_visible(
    pipeline_db, sqlite_ds_path, monkeypatch
):
    """Requirement B, end to end: when the scope check cannot complete, the
    answer must carry 'could not be verified' — never a silent pass, never a
    claim that the data is incomplete."""
    import uuid as _uuid

    import app.services.db.nl_answer_service as nls
    import app.services.db.query_router as qr
    import app.services.db.schema_classification as sc
    from app.models.knowledge_catalog import KBTableMeta

    make_source(pipeline_db, sqlite_ds_path)
    pipeline_db.add(KBTableMeta(
        id=str(_uuid.uuid4()), kb_id="kb-sqlite-1", schema_name="",
        table_name="customers", table_type="TABLE",
        description_en="customer records",
        dataset_scope="partition:region", dataset_scope_confidence=0.6,
        dataset_scope_signals_json={"partition_value": "north"},
    ))
    pipeline_db.commit()

    monkeypatch.setattr(settings, "KG_FRESHNESS_SHORTCIRCUIT_ENABLED", False)
    monkeypatch.setattr(settings, "SCHEMA_LINKING_ENABLED", True)
    monkeypatch.setattr(settings, "SEMANTIC_CATALOG_ENABLED", True)
    monkeypatch.setattr(settings, "SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False)
    monkeypatch.setattr(
        settings, "SCHEMA_VALIDATOR_DID_YOU_MEAN_ENABLED", False,
    )
    monkeypatch.setattr(settings, "NL2SQL_SHADOW_MODE_ENABLED", False)
    monkeypatch.setattr(settings, "NL2SQL_PLAN_MEMORY_ENABLED", False)
    monkeypatch.setattr(settings, "NL2SQL_DB_ERROR_REPAIR_MAX", 0)
    monkeypatch.setattr(
        settings, "SCHEMA_CLASSIFICATION_RETRIEVAL_ENABLED", True,
    )
    monkeypatch.setattr(
        settings, "SCHEMA_SCOPE_AUTHORIZATION_ENABLED", True,
    )
    monkeypatch.setattr(
        qr, "resolve_static_route", lambda *a, **k: None,
        raising=False,
    )
    monkeypatch.setattr(
        sc, "validate_sql_scope",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("catalog down")),
    )

    async def _fake_chat(messages, **kw):
        return '["customers"]'

    monkeypatch.setattr(nls, "_chat", _fake_chat)

    async def _text_to_sql(self, *args, **kwargs):
        return "SELECT id, name FROM customers"

    monkeypatch.setattr(
        "app.services.db.nl_answer_service.NLAnswerService._text_to_sql",
        _text_to_sql,
    )

    from app.services.nl2sql import ask

    r = ask("show me all customers across the whole company", kb_id="kb-sqlite-1", db=pipeline_db)

    assert r.success, r.error
    assert getattr(r, "scope_check", None) == "unavailable"
    assert "could not be verified" in r.answer, (
        "the degraded check must be visible in the streamed/persisted answer"
    )

