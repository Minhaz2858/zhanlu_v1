"""Phase 3.5 — enterprise observability & cost aggregation (TDD).

Reads ONLY existing tables (AgentInvocation, AgentRunStep, CostLedger,
ApprovalRequest, PolicyDecision) — no second telemetry system. Every query
is org-scoped (TimestampedBase.org_id isolation wall). Unknown model pricing
stays UNKNOWN (cost None) — never fabricated.

Run: pytest tests/test_usage_analytics.py -q
"""
from datetime import datetime, timedelta, timezone

import uuid

import pytest

from app.services.usage_analytics import agent_stats, org_overview


@pytest.fixture()
def seeded_db():
    from app.database import SessionLocal
    from app.models.agent_invocation import AgentInvocation
    from app.models.agent_run_step import AgentRunStep
    from app.models.governance import ApprovalRequest, PolicyDecision
    from app.models.agent_app import AgentApp
    from app.models.project import Project

    db = SessionLocal()
    now = datetime.now(timezone.utc)

    # Conftest truncates per MODULE; this module seeds per TEST — clear the
    # tables this fixture touches so counts are exact.
    for model in (AgentInvocation, AgentRunStep, ApprovalRequest, PolicyDecision,
                  AgentApp, Project):
        db.query(model).delete()
    db.flush()

    proj = Project(name="mfg-dept", org_id="org-1", department="manufacturing")
    db.add(proj)
    db.flush()
    app1 = AgentApp(name="prod_agent", project_id=proj.id, status="published", org_id="org-1")
    app2 = AgentApp(name="sales_agent", status="published", org_id="org-1")
    db.add_all([app1, app2])
    db.flush()

    def inv(agent_app_id, status, duration_ms, tokens, cost, days_ago=1, org="org-1"):
        return AgentInvocation(
            agent_app_id=agent_app_id, org_id=org,
            invocation_type="chat", status=status,
            started_at=now - timedelta(days=days_ago),
            completed_at=now - timedelta(days=days_ago),
            duration_ms=duration_ms,
            token_usage=tokens,
            cost_amount=cost,
        )

    db.add_all([
        inv(app1.id, "completed", 1200, {"prompt_tokens": 100, "completion_tokens": 50}, None),
        inv(app1.id, "failed", 800, {"prompt_tokens": 60, "completion_tokens": 10}, 0.02),
        inv(app1.id, "completed", 2000, {"prompt_tokens": 200, "completion_tokens": 80}, None),
        inv(app2.id, "completed", 900, {"prompt_tokens": 80, "completion_tokens": 40}, 0.01),
        inv(app2.id, "running", None, None, None),  # in-flight, excluded from latency
    ])
    db.flush()

    db.add_all([
        AgentRunStep(org_id="org-1", run_id="run-analytics", step_id=uuid.uuid4().hex[:16],
                     step_index=i, step_type="tool_call", tool_name=tn,
                     status=st, error=err)
        for i, (tn, st, err) in enumerate([
            ("execute_query", "failed", "connection refused"),
            ("execute_query", "completed", None),
            ("create_artifact", "failed", "invalid arguments"),
        ])
    ])

    db.add_all([
        ApprovalRequest(org_id="org-1", action_type="tool_call",
                        action_description="delete cronjob", risk_tier="high",
                        status="pending"),
        PolicyDecision(org_id="org-1", decision_level="gate", decision="deny",
                       risk_tier="high"),
    ])
    # Other tenant — must NEVER appear.
    db.add(inv(app1.id, "completed", 99999, {"prompt_tokens": 9999}, 9.99, org="org-2"))
    db.commit()
    yield db
    db.close()


def test_org_overview_counts_and_rates(seeded_db):
    ov = org_overview(seeded_db, org_id="org-1", days=30)
    assert ov["total_executions"] == 5  # other-tenant row excluded
    assert ov["by_status"]["completed"] == 3
    assert ov["by_status"]["failed"] == 1
    assert ov["by_status"]["running"] == 1
    # Latency excludes the in-flight invocation.
    assert ov["avg_duration_ms"] == round((1200 + 800 + 2000 + 900) / 4)


def test_org_overview_by_agent_and_department(seeded_db):
    ov = org_overview(seeded_db, org_id="org-1", days=30)
    by_agent = {a["agent"]: a for a in ov["by_agent"]}
    assert by_agent["prod_agent"]["executions"] == 3
    assert by_agent["sales_agent"]["executions"] == 2
    assert ov["by_department"].get("manufacturing") == 3


def test_tool_failure_distribution(seeded_db):
    ov = org_overview(seeded_db, org_id="org-1", days=30)
    dist = {d["tool_name"]: d["failures"] for d in ov["tool_failures"]}
    assert dist["execute_query"] == 1
    assert dist["create_artifact"] == 1


def test_token_and_cost_aggregation_honest_unknowns(seeded_db):
    ov = org_overview(seeded_db, org_id="org-1", days=30)
    assert ov["token_usage"]["prompt_tokens"] == 100 + 60 + 200 + 80
    assert ov["token_usage"]["completion_tokens"] == 50 + 10 + 80 + 40
    # Known-priced entries sum; UNKNOWN pricing stays unknown (None), never
    # fabricated into the total.
    assert ov["known_cost_total"] == pytest.approx(0.03)
    assert ov["unknown_pricing_executions"] >= 2


def test_governance_visibility(seeded_db):
    ov = org_overview(seeded_db, org_id="org-1", days=30)
    assert ov["approvals"]["pending"] == 1
    assert ov["unauthorized_operation_attempts"] == 1  # deny decisions


def test_tenant_isolation_no_cross_org_leak(seeded_db):
    ov = org_overview(seeded_db, org_id="org-2", days=30)
    # org-2 sees ONLY its own row — none of org-1's five.
    assert ov["total_executions"] == 1
    assert ov["token_usage"]["prompt_tokens"] == 9999
    assert ov["known_cost_total"] == pytest.approx(9.99)


def test_agent_stats_rollup(seeded_db):
    stats = agent_stats(seeded_db, org_id="org-1", days=30)
    by_agent = {s["agent"]: s for s in stats}
    pa = by_agent["prod_agent"]
    assert pa["executions"] == 3
    assert pa["success_rate"] == pytest.approx(0.6667, abs=1e-4)
    assert pa["avg_duration_ms"] == round((1200 + 800 + 2000) / 3)
