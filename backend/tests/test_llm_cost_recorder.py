"""LLM cost recorder tests — the wiring that makes hard-ROI costs real.

The governance layer always had CostService.record_llm_cost; nothing
called it. This suite covers the recorder: pricing via the existing
CostService table, tenant context propagation (incl. asyncio.to_thread,
which is how the FSM runs), and the no-op guards.
"""
import asyncio

import pytest


def _fresh_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/cost_test.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# ── pricing (reuses the existing CostService table) ──────────────────────


def test_unknown_model_gets_default_rate():
    from app.services.governance.cost_service import CostService

    db = _fresh_db(__import__("pathlib").Path("/tmp"))
    svc = CostService(db)
    cost = svc._calculate_llm_cost("totally-unknown-model", 1000, 1000)
    assert cost == pytest.approx(0.001 + 0.002)  # default 0.001/1K + 0.002/1K


# ── recorder behavior ─────────────────────────────────────────────────────


def test_recorder_writes_row_with_tenant_context(tmp_path, monkeypatch):
    from app.models.governance import CostLedger
    from app.services import llm_cost_recorder as rec

    db = _fresh_db(tmp_path)
    monkeypatch.setattr(rec, "SessionLocal", lambda: db)

    rec.set_llm_cost_context(
        org_id="org-a", agent_name="analyst",
        conversation_id="conv-1", app_id="app-1",
    )
    rec._safe_write("deepseek-chat", 1000, 500)

    row = db.query(CostLedger).first()
    assert row is not None
    assert row.org_id == "org-a"
    assert row.agent_name == "analyst"
    assert row.conversation_id == "conv-1"
    assert row.app_id == "app-1"
    assert row.prompt_tokens == 1000
    assert row.cost_amount > 0


def test_recorder_defaults_when_no_context(tmp_path, monkeypatch):
    from app.models.governance import CostLedger
    from app.services import llm_cost_recorder as rec

    db = _fresh_db(tmp_path)
    monkeypatch.setattr(rec, "SessionLocal", lambda: db)
    rec.llm_cost_context.set(None)
    rec._safe_write("m", 100, 100)

    row = db.query(CostLedger).first()
    assert row.org_id == "default-org"


def test_recorder_noop_on_zero_usage(tmp_path, monkeypatch):
    from app.models.governance import CostLedger
    from app.services import llm_cost_recorder as rec

    db = _fresh_db(tmp_path)
    monkeypatch.setattr(rec, "SessionLocal", lambda: db)
    rec.record_llm_usage_cost("m", 0, 0)
    assert db.query(CostLedger).count() == 0


def test_recorder_noop_when_disabled(tmp_path, monkeypatch):
    from app.models.governance import CostLedger
    from app.services import llm_cost_recorder as rec

    db = _fresh_db(tmp_path)
    monkeypatch.setattr(rec, "SessionLocal", lambda: db)
    monkeypatch.setattr(rec.settings, "COST_LEDGER_ENABLED", False)
    rec.record_llm_usage_cost("m", 100, 100)
    assert db.query(CostLedger).count() == 0


def test_recorder_never_raises_on_db_failure(tmp_path, monkeypatch):
    from app.services import llm_cost_recorder as rec

    def _boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(rec, "SessionLocal", _boom)
    # _safe_write swallows everything — bookkeeping must not break answers
    rec._safe_write("m", 10, 10)  # no exception


# ── context propagation through asyncio.to_thread (the FSM path) ─────────


def test_context_propagates_through_to_thread():
    from app.services.llm_cost_recorder import llm_cost_context, set_llm_cost_context

    set_llm_cost_context(org_id="org-x", agent_name="agent-x")
    seen = {}

    def _read_inside_thread():
        seen["ctx"] = llm_cost_context.get()

    asyncio.run(asyncio.to_thread(_read_inside_thread))
    assert seen["ctx"]["org_id"] == "org-x"
    assert seen["ctx"]["agent_name"] == "agent-x"


def test_roi_summary_counts_booked_cost(tmp_path, monkeypatch):
    """End-to-end at the data layer: recorder rows feed the ROI totals."""
    from app.services import llm_cost_recorder as rec
    from app.services.roi_service import roi_summary

    db = _fresh_db(tmp_path)
    monkeypatch.setattr(rec, "SessionLocal", lambda: db)
    rec.set_llm_cost_context(org_id="org-a", agent_name="analyst", conversation_id="c1")
    rec._safe_write("deepseek-chat", 10_000, 2_000)

    roi = roi_summary(db, org_id="org-a", days=30)
    assert roi["totals"]["cost_total"] > 0
    assert roi["totals"]["cost_by_type"]["llm_tokens"] > 0
    agents = {a["agent_name"]: a for a in roi["by_agent"]}
    assert agents["analyst"]["cost_total"] > 0
