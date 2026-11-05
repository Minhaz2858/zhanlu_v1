"""Phase 3.5 P1 — automatic node-level task resumption (TDD, deterministic).

`recover_interrupted_plans` closes the last Phase-3 gap: the resume-eligibility
layer (`durable_execution.resume_decision`) is now CONSUMED by a scheduled
recovery pass over persisted PlanNodes — reusing the existing scheduler,
advisory-lock single-worker guarantee, and ObservationRecord audit trail.
No new queue, no new scheduler, no LLM credits.

Rules under test:
- Completed/skipped tasks NEVER execute again.
- Stale read-only running tasks → re-eligible (pending) + audited.
- Stale approval-gated tasks → paused at the gate (pending), never executed.
- Stale DESTRUCTIVE (high-risk) tasks → honest 'failed' (write state unknown),
  NEVER auto-repeated.
- Dependency-unmet tasks do not resume.
- Duplicate recovery passes are idempotent (no double actions, no new
  AgentInvocation rows, evidence refs preserved).

Run: pytest tests/test_auto_resumption.py -q
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services.recovery_resumer import recover_interrupted_plans


@pytest.fixture()
def db():
    from app.database import SessionLocal
    from app.models.execution import Execution, ObservationRecord, PlanNode

    session = SessionLocal()
    session.query(ObservationRecord).delete()
    session.query(PlanNode).delete()
    session.query(Execution).delete()
    session.commit()
    yield session
    session.close()


def _stale(seconds=2000):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _fresh():
    return datetime.now(timezone.utc) - timedelta(seconds=30)


def _make_plan(db, nodes, execution_status="running"):
    """nodes: list of dicts {name, seq, status, started_at, risk_tier,
    requires_confirmation, deps:[seq], result, error}."""
    from app.models.execution import Execution, PlanNode

    exec_row = Execution(
        id=uuid.uuid4().hex, user_message="investigate declining sales",
        agent_name="general_assistant", current_state=execution_status,
    )
    db.add(exec_row)
    db.flush()
    by_seq = {}
    for spec in nodes:
        node = PlanNode(
            id=uuid.uuid4().hex,
            plan_id=exec_row.id,
            seq=spec["seq"],
            node_type=spec.get("node_type", "tool"),
            name=spec["name"],
            status=spec["status"],
            risk_tier=spec.get("risk_tier", "low"),
            requires_confirmation=spec.get("requires_confirmation", False),
            dependencies=[str(s) for s in spec.get("deps", [])],
            result=spec.get("result"),
            error=spec.get("error"),
            started_at=spec.get("started_at"),
            completed_at=spec.get("completed_at"),
        )
        db.add(node)
        by_seq[spec["seq"]] = node
    db.commit()
    return exec_row.id, by_seq


def test_worker_restart_recovers_stale_readonly_node(db):
    plan_id, nodes = _make_plan(db, [
        {"seq": 1, "name": "sales trend", "status": "completed",
         "started_at": _stale(), "result": {"findings": "declining"}},
        {"seq": 2, "name": "inventory check", "status": "running",
         "started_at": _stale()},
    ])
    summary = recover_interrupted_plans(db)
    db.refresh(nodes[2])
    assert nodes[2].status == "pending"  # re-eligible, NOT re-executed here
    assert nodes[1].status == "completed"  # preserved
    assert summary["recovered"] == 1
    assert summary["completed_preserved"] == 1
    # Recovery is AUDITABLE: an ObservationRecord with the decision exists.
    from app.models.execution import ObservationRecord

    obs = (
        db.query(ObservationRecord)
        .filter(ObservationRecord.plan_node_id == nodes[2].id)
        .all()
    )
    assert len(obs) == 1 and obs[0].observation_type == "recovery"
    assert obs[0].result_data["action"] == "recover"
    assert obs[0].result_data["idempotency_key"]


def test_completed_tasks_never_touched(db):
    plan_id, nodes = _make_plan(db, [
        {"seq": 1, "name": "done work", "status": "completed",
         "started_at": _stale(), "result": {"v": 1}},
        {"seq": 2, "name": "skipped work", "status": "skipped",
         "started_at": _stale()},
    ])
    summary = recover_interrupted_plans(db)
    db.refresh(nodes[1])
    db.refresh(nodes[2])
    assert nodes[1].status == "completed" and nodes[2].status == "skipped"
    assert summary["examined_running"] == 0


def test_live_running_node_within_ttl_left_alone(db):
    plan_id, nodes = _make_plan(db, [
        {"seq": 1, "name": "in flight", "status": "running", "started_at": _fresh()},
    ])
    summary = recover_interrupted_plans(db)
    db.refresh(nodes[1])
    assert nodes[1].status == "running"
    assert summary["left_running"] == 1


def test_approval_gated_stale_task_remains_paused_never_executed(db):
    plan_id, nodes = _make_plan(db, [
        {"seq": 1, "name": "submit po", "status": "running",
         "started_at": _stale(), "requires_confirmation": True,
         "risk_tier": "high"},
    ])
    summary = recover_interrupted_plans(db)
    db.refresh(nodes[1])
    # Paused at the approval gate (pending + requires_confirmation intact) —
    # recovery NEVER bypasses approval and NEVER executes the write.
    assert nodes[1].status == "pending"
    assert nodes[1].requires_confirmation is True
    assert summary["paused_approval"] == 1
    assert summary["auto_executed"] == 0


def test_destructive_stale_task_never_auto_repeated(db):
    plan_id, nodes = _make_plan(db, [
        {"seq": 1, "name": "reset schedule", "status": "running",
         "started_at": _stale(), "risk_tier": "high"},
    ])
    summary = recover_interrupted_plans(db)
    db.refresh(nodes[1])
    # Consequential write with unknown outcome → honest failed terminal,
    # never auto-repeated, requires manual verification.
    assert nodes[1].status == "failed"
    assert "manual" in (nodes[1].error or "").lower()
    assert summary["manual_verification"] == 1
    assert summary["auto_executed"] == 0


def test_dependency_unmet_task_does_not_resume(db):
    # A stale-running node whose dependency FAILED is un-stuck to 'pending'
    # (never permanently active) but is NOT counted as recovered — the
    # executor re-applies dependency logic before it can run.
    plan_id, nodes = _make_plan(db, [
        {"seq": 1, "name": "cost query", "status": "failed",
         "started_at": _stale(), "error": "permission denied: table x"},
        {"seq": 2, "name": "margin calc", "status": "running",
         "started_at": _stale(), "deps": [1]},
    ])
    summary = recover_interrupted_plans(db)
    db.refresh(nodes[2])
    assert nodes[2].status == "pending"  # un-stuck, waiting on deps
    assert summary["blocked_dependencies"] == 1
    assert summary["recovered"] == 0


def test_transient_failed_readonly_task_retried_within_budget(db):
    plan_id, nodes = _make_plan(db, [
        {"seq": 1, "name": "region query", "status": "failed",
         "started_at": _stale(), "completed_at": _stale(),
         "error": "connection refused"},
    ])
    summary = recover_interrupted_plans(db)
    db.refresh(nodes[1])
    assert nodes[1].status == "pending"
    assert summary["recovered"] == 1


def test_permission_failed_task_never_resumed(db):
    plan_id, nodes = _make_plan(db, [
        {"seq": 1, "name": "finance table", "status": "failed",
         "started_at": _stale(), "error": "Permission denied: table finances"},
    ])
    summary = recover_interrupted_plans(db)
    db.refresh(nodes[1])
    assert nodes[1].status == "failed"
    assert summary["recovered"] == 0


def test_duplicate_recovery_passes_are_idempotent(db):
    plan_id, nodes = _make_plan(db, [
        {"seq": 1, "name": "inv check", "status": "running", "started_at": _stale()},
    ])
    first = recover_interrupted_plans(db)
    second = recover_interrupted_plans(db)
    db.refresh(nodes[1])
    assert first["recovered"] == 1
    # Second pass: node is now pending (not running) → nothing new to do.
    assert second["examined_running"] == 0
    assert nodes[1].status == "pending"
    from app.models.execution import ObservationRecord

    audits = (
        db.query(ObservationRecord)
        .filter(ObservationRecord.plan_node_id == nodes[1].id)
        .count()
    )
    assert audits == 1


def test_recovery_creates_no_agent_invocation_rows(db):
    from app.models.agent_invocation import AgentInvocation

    before = db.query(AgentInvocation).count()
    _make_plan(db, [
        {"seq": 1, "name": "x", "status": "running", "started_at": _stale()},
    ])
    recover_interrupted_plans(db)
    recover_interrupted_plans(db)
    assert db.query(AgentInvocation).count() == before


def test_evidence_refs_survive_recovery(db):
    from app.models.execution import ObservationRecord

    plan_id, nodes = _make_plan(db, [
        {"seq": 1, "name": "sales query", "status": "running", "started_at": _stale()},
    ])
    db.add(ObservationRecord(
        id=uuid.uuid4().hex, execution_id=plan_id, plan_node_id=nodes[1].id,
        seq=0, observation_type="tool_call", tool_name="execute_query",
        success=True, result_data={"rows": [["A", 100]]},
        evidence_ids=["ev-1", "ev-2"],
    ))
    db.commit()
    recover_interrupted_plans(db)
    obs = (
        db.query(ObservationRecord)
        .filter(ObservationRecord.plan_node_id == nodes[1].id,
                ObservationRecord.observation_type == "tool_call")
        .first()
    )
    assert obs.evidence_ids == ["ev-1", "ev-2"]  # preserved, not rewritten


def test_partial_multi_agent_completion_recovery(db):
    # Sales completed, inventory stale-running, supply-chain failed-transient:
    # only eligible work is marked re-eligible; completed work untouched.
    plan_id, nodes = _make_plan(db, [
        {"seq": 1, "name": "sales", "status": "completed", "started_at": _stale(),
         "result": {"trend": "down"}},
        {"seq": 2, "name": "inventory", "status": "running", "started_at": _stale()},
        {"seq": 3, "name": "supply chain", "status": "failed",
         "started_at": _stale(), "completed_at": _stale(), "error": "timeout"},
    ])
    summary = recover_interrupted_plans(db)
    db.refresh(nodes[2])
    db.refresh(nodes[3])
    assert nodes[2].status == "pending"
    assert nodes[3].status == "pending"
    assert summary["recovered"] == 2
    assert summary["completed_preserved"] == 1


def test_recovery_event_visible_after_reconnection(db):
    # "Reconnection" = a fresh session querying persisted state.
    plan_id, nodes = _make_plan(db, [
        {"seq": 1, "name": "inv", "status": "running", "started_at": _stale()},
    ])
    recover_interrupted_plans(db)
    db.expire_all()
    from app.models.execution import ObservationRecord

    events = (
        db.query(ObservationRecord)
        .filter(ObservationRecord.execution_id == plan_id,
                ObservationRecord.observation_type == "recovery")
        .all()
    )
    assert len(events) == 1
    assert events[0].result_data["action"] in ("recover", "pending")


def test_scheduled_job_registered_with_gate():
    import inspect

    from app.services import scheduled_tasks as st

    src = inspect.getsource(st)
    assert "recover_interrupted_plans" in src
    assert "PLAN_NODE_RECOVERY" in src  # interval constant + settings gate
