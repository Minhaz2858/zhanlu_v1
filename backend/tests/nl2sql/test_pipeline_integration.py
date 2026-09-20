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
