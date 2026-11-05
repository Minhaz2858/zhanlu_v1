"""Automatic node-level task resumption (Phase 3.5 P1, 2026-10-31).

ROOT CAUSE ADDRESSED
--------------------
Phase 3 shipped the resume-eligibility decision layer
(``durable_execution.resume_decision``) but nothing CONSUMED it: after a
worker restart, stale running PlanNodes stayed running forever (the P2 audit
question "can an interrupted task remain permanently active?"). This module
is the scheduled recovery pass that closes that gap — reusing the EXISTING
scheduler (asyncio periodic job + Postgres advisory lock single-worker
guarantee), the EXISTING durable state (PlanNode/ObservationRecord), and the
EXISTING decision layer. No new queue, no second scheduler, no LLM calls.

Behavior per STALE running PlanNode (``started_at`` older than
``STALE_RUNNING_TTL_S``):

- completed / skipped        → never touched (no duplicate execution)
- running within TTL         → left alone (a live worker owns it)
- requires_confirmation      → paused at the approval gate (status pending,
                               flag intact) — recovery NEVER bypasses approval
- risk_tier == "high" (destructive) → honest ``failed`` ("consequential
  write state unknown — manual verification required") — never auto-repeated
- read-only running          → ``pending`` (re-eligible under its idempotency key)
- failed + transient error   → ``pending`` (bounded retry; permission errors stay failed)
- dependency-unmet downstream nodes → recorded as blocked, never resumed

Every action writes an audit ObservationRecord (observation_type="recovery")
carrying the decision, idempotency key and reason — visible after
reconnection. The pass NEVER executes tools, NEVER creates AgentInvocation
rows, and is idempotent across repeated runs.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.services.durable_execution import (
    STALE_RUNNING_TTL_S,
    is_stale,
    resume_decision,
    task_idempotency_key,
)

_TRANSIENT_MARKERS = (
    "timeout", "timed out", "connection", "refused", "reset", "unreachable",
    "rate limit", "429", "temporar", "deadlock", "lock wait", "超时", "连接",
    "暂时",
)


def _audit(db: Session, execution_id: str, node, decision: dict) -> None:
    """Persist the recovery decision as a first-class execution event."""
    from app.models.execution import ObservationRecord

    db.add(ObservationRecord(
        id=uuid.uuid4().hex,
        execution_id=execution_id,
        plan_node_id=node.id,
        seq=_next_seq(db, execution_id),
        observation_type="recovery",
        tool_name="recovery_resumer",
        success=decision.get("action") != "manual_verification",
        result_data=decision,
    ))


def _next_seq(db: Session, execution_id: str) -> int:
    from app.models.execution import ObservationRecord

    return (
        db.query(ObservationRecord)
        .filter(ObservationRecord.execution_id == execution_id)
        .count()
    )


def recover_interrupted_plans(
    db: Session,
    *,
    stale_ttl_s: int = STALE_RUNNING_TTL_S,
    max_nodes: int = 100,
) -> dict:
    """One recovery pass over persisted plan nodes. Idempotent, auditable,
    deterministic. Returns a summary counter dict (for logs/metrics)."""
    from app.models.execution import PlanNode

    now = datetime.now(timezone.utc)
    running_nodes = (
        db.query(PlanNode)
        .filter(PlanNode.status == "running")
        .order_by(PlanNode.started_at.asc())
        .limit(max_nodes)
        .all()
    )
    # Recently-failed transient read-only nodes are retry-eligible (mission:
    # "failed read-only tasks may be retried when safe") — window-bounded so
    # old failures are never resurrected.
    failed_nodes = (
        db.query(PlanNode)
        .filter(
            PlanNode.status == "failed",
            PlanNode.risk_tier != "high",
            PlanNode.requires_confirmation == False,  # noqa: E712
            PlanNode.completed_at >= now - timedelta(seconds=stale_ttl_s * 2),
        )
        .limit(max_nodes)
        .all()
    )

    summary = {
        "examined_running": 0,
        "left_running": 0,
        "completed_preserved": 0,
        "recovered": 0,
        "paused_approval": 0,
        "manual_verification": 0,
        "blocked_dependencies": 0,
        "auto_executed": 0,  # ALWAYS 0 — the pass never executes anything
    }

    # Completion status of every node in the affected plans (dependency checks).
    plan_ids = {n.plan_id for n in running_nodes} | {
        n.plan_id for n in failed_nodes
    }
    all_nodes = (
        db.query(PlanNode).filter(PlanNode.plan_id.in_(plan_ids)).all()
        if plan_ids else []
    )
    status_by_id = {n.id: n.status for n in all_nodes}

    for node in running_nodes:
        summary["examined_running"] += 1
        if not is_stale(node.started_at, now=now, ttl_s=stale_ttl_s):
            summary["left_running"] += 1
            continue

        # Dependency gate: a stale node whose dependencies are unmet must not
        # resume (its inputs may never arrive). It is un-stuck to 'pending'
        # (the neutral waiting state) — never executed, executor re-applies
        # dependency logic.
        dep_ids = [str(d) for d in (node.dependencies or [])]
        unmet = [
            d for d in dep_ids
            if status_by_id.get(d) not in ("completed", "skipped")
        ]
        if unmet:
            node.status = "pending"
            node.started_at = None
            summary["blocked_dependencies"] += 1
            _audit(db, node.plan_id, node, {
                "action": "blocked_dependencies",
                "reason": "dependencies not satisfied — recovery withheld",
                "unmet_dependencies": unmet[:5],
                "idempotency_key": task_idempotency_key(
                    node.plan_id, node.seq, node.name),
            })
            continue

        mutating = (node.risk_tier or "low") == "high"
        decision = resume_decision(
            status="running",
            started_at=node.started_at,
            error=node.error or "",
            mutating=mutating,
            requires_confirmation=bool(node.requires_confirmation),
            plan_id=node.plan_id,
            seq=node.seq,
            name=node.name,
            now=now,
        )

        if decision.action == "manual" and node.requires_confirmation:
            # Paused at the approval gate — never bypassed, never executed.
            node.status = "pending"
            action_label = "paused_approval"
            summary["paused_approval"] += 1
        elif decision.action == "manual":
            # Destructive/consequential write with unknown outcome: honest
            # terminal failure — never auto-repeated, requires verification.
            node.status = "failed"
            node.error = (
                "interrupted before a consequential write completed — "
                "resource state unknown; manual verification required "
                f"(idempotency_key={decision.idempotency_key[:16]})"
            )
            node.completed_at = now
            action_label = "manual_verification"
            summary["manual_verification"] += 1
        else:  # recover → re-eligible
            node.status = "pending"
            node.started_at = None
            action_label = "recover"
            summary["recovered"] += 1

        _audit(db, node.plan_id, node, {
            "action": action_label,
            "reason": decision.reason,
            "idempotency_key": decision.idempotency_key,
            "requires_authorization": decision.requires_authorization,
            "risk_tier": node.risk_tier,
        })

    # Recently-failed transient read-only nodes → re-eligible (bounded retry;
    # idempotent — after this pass they are pending, not failed).
    for node in failed_nodes:
        err_low = (node.error or "").lower()
        if not any(m in err_low for m in _TRANSIENT_MARKERS):
            continue
        dep_ids = [str(d) for d in (node.dependencies or [])]
        unmet = [
            d for d in dep_ids
            if status_by_id.get(d) not in ("completed", "skipped")
        ]
        if unmet:
            summary["blocked_dependencies"] += 1
            continue
        node.status = "pending"
        summary["recovered"] += 1
        _audit(db, node.plan_id, node, {
            "action": "recover",
            "reason": "transient failure — retry eligible within the healing budget",
            "idempotency_key": task_idempotency_key(
                node.plan_id, node.seq, node.name),
            "risk_tier": node.risk_tier,
        })

    # Completion-preservation accounting (for truthful summaries): count
    # terminal nodes in the affected plans that were NOT touched.
    summary["completed_preserved"] = sum(
        1 for n in all_nodes if n.status in ("completed", "skipped")
    )
    db.commit()
    return summary
