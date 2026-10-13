"""ROI service tests (IBM enterprise-AI Gap 1, 2026-10-13).

The ROI layer links existing operational data (cost ledger, conversations,
artifacts) into business-value metrics per org/agent — hard ROI (cost,
deliverables) plus a clearly-labeled hours-saved ESTIMATE.
"""
import pytest


def _fresh_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401 — register every model on Base.metadata
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/roi_test.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_conversation(db, org_id, agent_name, n_user_turns, n_assistant,
                       messages=None, days_ago=0):
    """Seed an AgentConversation row with a messages JSON list."""
    import json
    from datetime import datetime, timedelta, timezone

    from app.models.agent_conversation import AgentConversation

    msgs = messages if messages is not None else (
        [{"role": "user", "content": f"q{i}"} for i in range(n_user_turns)]
        + [{"role": "assistant", "content": "answer"} for i in range(n_assistant)]
    )
    conv = AgentConversation(
        agent_name=agent_name,
        messages=json.dumps(msgs) if isinstance(msgs, list) else msgs,
        org_id=org_id,
        created_date=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _seed_cost(db, org_id, agent_name, amount, cost_type="llm_tokens", days_ago=0):
    from datetime import datetime, timedelta, timezone

    from app.models.governance import CostLedger

    row = CostLedger(
        org_id=org_id,
        agent_name=agent_name,
        cost_type=cost_type,
        cost_amount=amount,
        recorded_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(row)
    db.commit()
    return row


def test_roi_summary_scopes_to_org(tmp_path):
    """Org A's ROI must never include org B's conversations or costs."""
    from app.services.roi_service import roi_summary

    db = _fresh_db(tmp_path)
    _seed_conversation(db, "org-a", "analyst", n_user_turns=4, n_assistant=4)
    _seed_cost(db, "org-a", "analyst", 5.0)
    _seed_conversation(db, "org-b", "analyst", n_user_turns=10, n_assistant=10)
    _seed_cost(db, "org-b", "analyst", 50.0)

    roi = roi_summary(db, org_id="org-a", days=30)
    assert roi["totals"]["conversations"] == 1
    assert roi["totals"]["user_turns"] == 4
    assert roi["totals"]["cost_total"] == pytest.approx(5.0)
    agents = {a["agent_name"]: a for a in roi["by_agent"]}
    assert set(agents) == {"analyst"}


def test_roi_summary_estimates_hours_saved(tmp_path):
    """Hard-ROI estimate: user turns × per-turn minutes, labeled as such."""
    from app.services.roi_service import roi_summary

    db = _fresh_db(tmp_path)
    _seed_conversation(db, "org-a", "analyst", n_user_turns=6, n_assistant=6)
    _seed_cost(db, "org-a", "analyst", 2.0)

    roi = roi_summary(db, org_id="org-a", days=30)
    est = roi["totals"]["est_hours_saved"]
    # 6 turns × 10 min = 60 min = 1.0 hour (default constant)
    assert est == pytest.approx(1.0)
    assert roi["totals"]["est_assumptions"]["minutes_per_turn"] > 0


def test_roi_summary_window_excludes_old_rows(tmp_path):
    """Rows older than the window are excluded."""
    from app.services.roi_service import roi_summary

    db = _fresh_db(tmp_path)
    _seed_conversation(db, "org-a", "analyst", n_user_turns=2, n_assistant=1,
                       days_ago=90)
    _seed_cost(db, "org-a", "analyst", 3.0, days_ago=90)

    roi = roi_summary(db, org_id="org-a", days=30)
    assert roi["totals"]["conversations"] == 0
    assert roi["totals"]["cost_total"] == 0.0


def test_roi_summary_by_agent_breakdown(tmp_path):
    from app.services.roi_service import roi_summary

    db = _fresh_db(tmp_path)
    _seed_conversation(db, "org-a", "data_analyst", n_user_turns=2, n_assistant=2)
    _seed_conversation(db, "org-a", "report_writer", n_user_turns=1, n_assistant=1)
    _seed_cost(db, "org-a", "data_analyst", 1.0)
    _seed_cost(db, "org-a", "report_writer", 3.0)

    roi = roi_summary(db, org_id="org-a", days=30)
    agents = {a["agent_name"]: a for a in roi["by_agent"]}
    assert agents["data_analyst"]["user_turns"] == 2
    assert agents["report_writer"]["cost_total"] == pytest.approx(3.0)
    # agents are sorted by cost desc — the CoE "where does the money go" view
    assert roi["by_agent"][0]["agent_name"] == "report_writer"


def test_roi_summary_handles_missing_conversation_columns(tmp_path):
    """Conversations with non-list messages JSON are skipped, not crashed."""
    from app.services.roi_service import roi_summary

    db = _fresh_db(tmp_path)
    _seed_conversation(db, "org-a", "weird", messages="not-a-json-list",
                       n_user_turns=0, n_assistant=0)

    roi = roi_summary(db, org_id="org-a", days=30)
    assert roi["totals"]["conversations"] == 1
    assert roi["totals"]["user_turns"] == 0


def test_roi_summary_empty_org(tmp_path):
    from app.services.roi_service import roi_summary

    db = _fresh_db(tmp_path)
    roi = roi_summary(db, org_id="org-nothing", days=30)
    assert roi["totals"]["conversations"] == 0
    assert roi["totals"]["user_turns"] == 0
    assert roi["totals"]["cost_total"] == 0.0
    assert roi["totals"]["est_hours_saved"] == 0.0
    assert roi["by_agent"] == []
