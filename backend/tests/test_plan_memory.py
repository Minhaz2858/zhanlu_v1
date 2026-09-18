"""Plan memory — one business question must not answer differently each run.

The failure this covers (2026-09-18, live, aipdp_data_warehouse_prod): "contract
execution status" resolved to FCLOSESTATUS on one run and FDOCUMENTSTATUS on the
next. Both plausible, only one right, two different numbers for the same
business question. Nothing in the platform remembered how a question had been
answered, so it was re-derived (and re-guessed) every turn.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.query_plan_memory import QueryPlanMemory
from app.services.db.plan_memory import (
    format_plan_guidance,
    load_plan,
    mark_plan_used,
    question_signature,
    save_plan,
)

KB = "kb-1"


@pytest.fixture()
def db():
    """Only this model's table — the shared sqlite fixtures carry unrelated DDL."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    QueryPlanMemory.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Signature: same intent → same key; different period → different key
# ---------------------------------------------------------------------------

def test_signature_ignores_case_punctuation_and_whitespace():
    assert question_signature("Contract Status, Last Month!") == question_signature(
        "contract   status   last month"
    )


def test_signature_ignores_numbers_so_top_n_shares_one_plan():
    assert question_signature("top 10 customers by revenue") == question_signature(
        "top 5 customers by revenue"
    )


def test_signature_keeps_the_period_phrase():
    """A different window is a different question — the plan must not be reused."""
    assert question_signature("sales for last month") != question_signature(
        "sales for last week"
    )


def test_signature_is_stable_and_short_enough_for_the_column():
    sig = question_signature("按客户统计上月合同状态")
    assert sig == question_signature("按客户统计上月合同状态")
    assert len(sig) == 64


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_save_then_load_returns_the_plan(db):
    assert save_plan(
        db, KB, "contract status for last month",
        tables=["erp_t_crm_contract"],
        sql="SELECT FDOCUMENTSTATUS, COUNT(*) n FROM erp_t_crm_contract "
            "WHERE FDATE >= '2026-08-01' GROUP BY FDOCUMENTSTATUS",
        dimensions=["FDOCUMENTSTATUS"],
    ) is True

    plan = load_plan(db, KB, "contract status for last month")
    assert plan is not None
    assert plan["tables"] == ["erp_t_crm_contract"]
    assert plan["dimensions"] == ["FDOCUMENTSTATUS"]
    assert "GROUP BY FDOCUMENTSTATUS" in plan["sql"]
    assert plan["hits"] == 0


def test_load_misses_on_a_different_question(db):
    save_plan(db, KB, "sales by customer", tables=["t"], sql="SELECT 1 FROM t")
    assert load_plan(db, KB, "sales by supplier") is None


def test_load_is_scoped_per_knowledge_base(db):
    save_plan(db, KB, "sales by customer", tables=["t"], sql="SELECT 1 FROM t")
    assert load_plan(db, "other-kb", "sales by customer") is None


def test_resaving_refreshes_instead_of_duplicating(db):
    save_plan(db, KB, "q", tables=["a"], sql="SELECT 1 FROM a")
    save_plan(db, KB, "q", tables=["b"], sql="SELECT 2 FROM b")
    assert db.query(QueryPlanMemory).count() == 1
    assert load_plan(db, KB, "q")["tables"] == ["b"]


def test_blank_sql_is_never_stored(db):
    assert save_plan(db, KB, "q", tables=["a"], sql="   ") is False
    assert load_plan(db, KB, "q") is None


def test_mark_used_counts_reuse(db):
    save_plan(db, KB, "q", tables=["a"], sql="SELECT 1 FROM a")
    plan = load_plan(db, KB, "q")
    mark_plan_used(db, plan)
    mark_plan_used(db, plan)
    assert load_plan(db, KB, "q")["hits"] == 2


def test_mark_used_tolerates_no_plan(db):
    mark_plan_used(db, None)          # must not raise
    mark_plan_used(db, {"id": "nope"})


def test_store_failures_degrade_to_no_plan():
    """A missing table (first boot before create_all) must not break answering."""
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("no such table: query_plan_memory")

    class _BoomSession:
        def query(self, *a, **k):
            raise RuntimeError("no such table: query_plan_memory")

        def rollback(self):
            pass

    assert load_plan(_BoomSession(), KB, "q") is None
    assert save_plan(_BoomSession(), KB, "q", ["a"], "SELECT 1") is False


# ---------------------------------------------------------------------------
# Guidance text — the part that stops the drift
# ---------------------------------------------------------------------------

def test_guidance_carries_tables_grouping_and_the_verified_sql():
    text = format_plan_guidance({
        "tables": ["erp_t_crm_contract"],
        "dimensions": ["FDOCUMENTSTATUS"],
        "sql": "SELECT FDOCUMENTSTATUS FROM erp_t_crm_contract",
    })
    assert "erp_t_crm_contract" in text
    assert "FDOCUMENTSTATUS" in text
    assert "Re-resolve any relative period" in text


def test_guidance_forbids_copying_stale_dates():
    text = format_plan_guidance({"tables": ["t"], "sql": "SELECT 1 FROM t WHERE d > '2026-08-01'"})
    assert "never copy a date literal" in text


def test_guidance_is_bounded_and_empty_without_a_plan():
    assert format_plan_guidance(None) == ""
    text = format_plan_guidance({"tables": ["t"], "sql": "SELECT " + "x" * 5000})
    assert len(text) < 1500
