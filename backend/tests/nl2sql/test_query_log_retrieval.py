"""Query Log Retrieval — OCI online stage: similar prior questions as grounding.

Pins: only successful executions are eligible; reads are strictly kb-scoped;
the current question's own signature is excluded (plan memory owns the exact
match); ranking is lexical overlap over the question, never popularity alone;
the prompt block is bounded; the whole thing is flag-gated and fail-soft.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import TimestampedBase
from app.models.nl2sql_query_log import Nl2sqlQueryLog
from app.models.query_plan_memory import QueryPlanMemory
from app.services.db.query_log_retrieval import (
    build_query_log_guidance,
    format_query_log_guidance,
    retrieve_similar_questions,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TimestampedBase.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _log(session, question, sql, *, kb="kb1", outcome="success", n=1):
    for _ in range(n):
        session.add(
            Nl2sqlQueryLog(
                datasource_id=kb,
                question=question,
                generated_sql=sql,
                outcome=outcome,
                row_count=3,
            )
        )
    session.commit()


# ── eligibility ───────────────────────────────────────────────────────────


def test_only_successful_rows_are_retrieved(db):
    _log(db, "top customers last month", "SELECT name FROM customers", outcome="success")
    _log(db, "top customers last year", "SELECT name FROM customers", outcome="error")
    _log(db, "top customers this week", "SELECT name FROM customers", outcome="denied")
    hits = retrieve_similar_questions(
        db, "kb1", "who are the top customers for last month"
    )
    assert hits, "the successful row must be retrievable"
    assert all(h["outcome"] == "success" for h in hits)


def test_strictly_kb_scoped(db):
    _log(db, "contract execution last month", "SELECT * FROM contracts", kb="kb1")
    _log(db, "contract execution last week", "SELECT * FROM other", kb="kb2")
    hits = retrieve_similar_questions(db, "kb1", "contract execution this month")
    assert all(h.get("source") != "kb2" for h in hits)
    assert all("other" not in (h.get("sql") or "") for h in hits)


def test_identical_signature_excluded(db):
    q = "sales report for last month"
    _log(db, q, "SELECT 1")
    hits = retrieve_similar_questions(db, "kb1", q)
    assert hits == [], "the exact question is plan memory's job, not retrieval's"


def test_rows_without_sql_are_skipped(db):
    _log(db, "top customers last month", None)
    assert retrieve_similar_questions(db, "kb1", "top customers last month") == []


# ── ranking ───────────────────────────────────────────────────────────────


def test_similar_ranked_above_unrelated(db):
    _log(db, "monthly revenue trend by region", "SELECT region, SUM(rev) FROM sales")
    _log(db, "employee birthday list", "SELECT name FROM hr_employees")
    hits = retrieve_similar_questions(db, "kb1", "revenue trend this month by region")
    assert hits
    assert "revenue" in hits[0]["question"].lower()


def test_limit_respected(db):
    for i in range(8):
        _log(db, f"top customers variant {i} last month", f"SELECT {i} FROM customers")
    hits = retrieve_similar_questions(db, "kb1", "top customers last month", limit=3)
    assert len(hits) <= 3


# ── verified plan memory participates ─────────────────────────────────────


def test_verified_plan_row_is_included(db):
    db.add(
        QueryPlanMemory(
            kb_id="kb1",
            signature="sig-other",
            question="contract execution status last month",
            tables=["erp_v_contract_execution"],
            sql="SELECT status FROM erp_v_contract_execution",
            dimensions=["status"],
            hits=5,
        )
    )
    db.commit()
    hits = retrieve_similar_questions(db, "kb1", "contract execution status this month")
    assert any(h["source"] == "plan_memory" for h in hits)


# ── the prompt block ──────────────────────────────────────────────────────


def test_format_guidance_bounded_and_useful(db):
    _log(db, "top customers by revenue last month", "SELECT name FROM customers " + "x" * 900)
    entries = retrieve_similar_questions(db, "kb1", "top customers last month")
    block = format_query_log_guidance(entries)
    assert block
    assert len(block) <= 2000
    assert "top customers" in block


def test_format_guidance_empty_when_no_entries():
    assert format_query_log_guidance([]) == ""


def test_flag_gate(monkeypatch, db):
    from app.config import settings

    _log(db, "top customers by revenue last month", "SELECT name FROM customers")
    monkeypatch.setattr(settings, "NL2SQL_QUERY_LOG_RETRIEVAL_ENABLED", False)
    assert build_query_log_guidance(db, "kb1", "top customers last month") == ""
    monkeypatch.setattr(settings, "NL2SQL_QUERY_LOG_RETRIEVAL_ENABLED", True)
    assert build_query_log_guidance(db, "kb1", "top customers last month") != ""


def test_never_raises_on_bad_input(db):
    assert retrieve_similar_questions(db, "", "  ") == []
    assert retrieve_similar_questions(None, "kb1", "q") == []
    assert build_query_log_guidance(db, "", "") == ""
