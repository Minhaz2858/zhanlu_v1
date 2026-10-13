"""Use-case portfolio tests (IBM Gap 2, 2026-10-13).

The AI-CoE board: every agent with business owner/KPI/lifecycle stage
plus its measured ROI — org-scoped, stage-validated.
"""


def _fresh_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/portfolio_test.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_agent(db, org_id, name):
    from app.models.agent_app import AgentApp

    a = AgentApp(name=name, org_id=org_id, status="published")
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def test_upsert_creates_and_updates(tmp_path):
    from app.services.usecase_portfolio import upsert_entry

    db = _fresh_db(tmp_path)
    e = upsert_entry(
        db, "org-a", "agent-1",
        agent_name="Quote Analyst",
        business_owner="Sales Ops",
        business_kpi="quote turnaround time",
        lifecycle_stage="pilot",
    )
    assert e.lifecycle_stage == "pilot"

    e2 = upsert_entry(
        db, "org-a", "agent-1", lifecycle_stage="production",
        business_owner="Sales Director",
    )
    assert e2.id == e.id  # same row updated, not duplicated
    assert e2.lifecycle_stage == "production"
    assert e2.business_owner == "Sales Director"
    assert e2.business_kpi == "quote turnaround time"  # untouched field kept


def test_upsert_rejects_unknown_stage(tmp_path):
    """The service raises ValueError; the router maps it to 422."""
    from app.services.usecase_portfolio import upsert_entry

    db = _fresh_db(tmp_path)
    try:
        upsert_entry(db, "org-a", "agent-1", lifecycle_stage="banana")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_portfolio_scopes_to_org_and_includes_roi(tmp_path):
    from app.services.usecase_portfolio import get_portfolio

    db = _fresh_db(tmp_path)
    a1 = _seed_agent(db, "org-a", "Quote Analyst")
    _seed_agent(db, "org-b", "Other Tenant Agent")

    from app.services.usecase_portfolio import upsert_entry
    upsert_entry(db, "org-a", a1.id, agent_name="Quote Analyst",
                 lifecycle_stage="production",
                 business_owner="Sales Ops", business_kpi="turnaround")

    # ROI activity for org-a's agent
    import json
    from app.models.agent_conversation import AgentConversation
    conv = AgentConversation(
        agent_name="Quote Analyst",
        messages=json.dumps([
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a", "artifact_ids": ["x"]},
        ]),
        org_id="org-a",
    )
    db.add(conv)
    db.commit()

    board = get_portfolio(db, org_id="org-a", days=30)
    items = {i["agent_name"]: i for i in board["items"]}
    qa = items["Quote Analyst"]
    assert qa["lifecycle_stage"] == "production"
    assert qa["business_owner"] == "Sales Ops"
    assert qa["roi"]["user_turns"] == 1
    assert qa["roi"]["artifacts_created"] == 1
    assert "Other Tenant Agent" not in items


def test_agents_without_entry_default_to_idea_stage(tmp_path):
    from app.services.usecase_portfolio import get_portfolio

    db = _fresh_db(tmp_path)
    _seed_agent(db, "org-a", "Fresh Agent")

    board = get_portfolio(db, org_id="org-a", days=30)
    fresh = [i for i in board["items"] if i["agent_name"] == "Fresh Agent"][0]
    assert fresh["lifecycle_stage"] == "idea"
    assert fresh["business_owner"] is None


def test_summary_counts_by_stage(tmp_path):
    from app.services.usecase_portfolio import get_portfolio, upsert_entry

    db = _fresh_db(tmp_path)
    a1 = _seed_agent(db, "org-a", "A")
    a2 = _seed_agent(db, "org-a", "B")
    upsert_entry(db, "org-a", a1.id, lifecycle_stage="production")
    upsert_entry(db, "org-a", a2.id, lifecycle_stage="pilot")

    board = get_portfolio(db, org_id="org-a", days=30)
    assert board["summary"]["production"] == 1
    assert board["summary"]["pilot"] == 1
