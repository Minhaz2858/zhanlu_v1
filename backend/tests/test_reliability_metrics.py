"""Phase 3.4 — platform reliability & performance metrics (TDD).

Aggregates the mission's metric table from EXISTING records (AgentInvocation,
AgentRunStep, EvalResult). Deterministic; the provider probe is mocked in
tests and run live only out-of-band.

Run: pytest tests/test_reliability_metrics.py -q
"""
import uuid

from datetime import datetime, timedelta, timezone

import pytest

from app.services.reliability_metrics import platform_reliability_report, provider_status


@pytest.fixture()
def seeded_db():
    from app.database import SessionLocal
    from app.models.agent_invocation import AgentInvocation
    from app.models.agent_run_step import AgentRunStep

    db = SessionLocal()
    db.query(AgentInvocation).delete()
    db.query(AgentRunStep).delete()
    db.flush()
    now = datetime.now(timezone.utc)

    def inv(status, outcome=None, duration_ms=1000, tokens=None, days_ago=1,
            recovery=False):
        return AgentInvocation(
            agent_app_id="app-x", org_id="org-r", invocation_type="chat",
            status=status, outcome=outcome,
            started_at=now - timedelta(days=days_ago),
            completed_at=now - timedelta(days=days_ago),
            duration_ms=duration_ms,
            token_usage=tokens or {},
            output_json={"recovery": {"reason": "worker_lost"}} if recovery else None,
        )

    db.add_all([
        inv("completed", outcome="success", duration_ms=1000,
            tokens={"prompt_tokens": 100, "completion_tokens": 40}),
        inv("completed", outcome="success", duration_ms=2000,
            tokens={"prompt_tokens": 150, "completion_tokens": 60}),
        inv("failed", outcome="fail", duration_ms=500,
            tokens={"prompt_tokens": 50, "completion_tokens": 0}, recovery=True),
        inv("completed", outcome="success", duration_ms=1500,
            tokens={"prompt_tokens": 80, "completion_tokens": 30}, recovery=True),
    ])
    db.flush()
    for i, (tn, st) in enumerate([
        ("execute_query", "completed"),
        ("execute_query", "failed"),
        ("ask_data_agent", "completed"),
        ("run_sandbox_skill", "failed"),
    ]):
        db.add(AgentRunStep(org_id="org-r", run_id="run-rel", step_id=uuid.uuid4().hex,
                            step_index=i, step_type="tool_call", tool_name=tn,
                            status=st))
    db.commit()
    yield db
    db.close()


def test_completion_and_rates(seeded_db):
    r = platform_reliability_report(seeded_db, org_id="org-r", days=30)
    assert r["task_completion_rate"] == pytest.approx(3 / 4)
    assert r["tool_success_rate"] == pytest.approx(2 / 4)
    assert r["recovery_frequency"] == 2


def test_latency_and_tokens(seeded_db):
    r = platform_reliability_report(seeded_db, org_id="org-r", days=30)
    assert r["avg_latency_ms"] == round((1000 + 2000 + 500 + 1500) / 4)
    assert r["token_usage"]["prompt_tokens"] == 100 + 150 + 50 + 80
    assert r["token_usage"]["completion_tokens"] == 40 + 60 + 0 + 30


def test_delegation_frequency(seeded_db):
    # Delegations counted from tool steps that are delegation tools.
    r = platform_reliability_report(seeded_db, org_id="org-r", days=30)
    assert r["delegation_frequency"] == 1  # ask_data_agent step


def test_recovery_duplication_tracked(seeded_db):
    r = platform_reliability_report(seeded_db, org_id="org-r", days=30)
    assert "recovery_duplication_rate" in r
    assert 0.0 <= r["recovery_duplication_rate"] <= 1.0


def test_unknown_pricing_not_estimated(seeded_db):
    r = platform_reliability_report(seeded_db, org_id="org-r", days=30)
    assert r["execution_cost"]["known_total"] == 0.0
    assert r["execution_cost"]["unknown_pricing_executions"] == 4


def test_provider_status_shape_no_secrets():
    status = provider_status()
    # Shape contract: booleans + string reason; NEVER contains a key.
    assert isinstance(status, dict)
    assert {"configured", "available"} <= set(status.keys())
    blob = str(status)
    assert "sk-" not in blob and "api_key" not in blob.lower()
