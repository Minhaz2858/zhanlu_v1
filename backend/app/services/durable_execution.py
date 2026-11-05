"""Durable execution & task recovery decisions (Phase 3.1, 2026-10-31).

ROOT CAUSE ADDRESSED
--------------------
PlanNode results and ObservationRecords ARE already durable (Phase-0/2 infra),
but nothing DECIDED what may safely resume after a worker restart: completed
work could be re-executed (duplicates), stale running nodes had no owner, and
mutating/approval-gated work had no explicit no-auto-rerun rule.

This module is the pure RESUME DECISION LAYER on top of the existing durable
state (PlanNode.status/result, ObservationRecord, AgentRunStep checkpoints,
recover_orphaned_invocations). It never executes anything, never bypasses
permissions or approvals, and never repeats consequential writes:

- completed / skipped  → skip (no duplicate execution)
- running within TTL   → leave running (a live worker owns it)
- running STALE        → recover ONLY if read-only; manual otherwise
- failed + transient   → retry eligible, with a deterministic idempotency key
- failed + terminal    → manual (authorization boundary — never auto-resumed)
- approval-gated       → manual ALWAYS (recovery never bypasses approval)

The idempotency key is a deterministic hash of (plan, seq, name) so a retried
node can be deduped downstream by existing mechanisms.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

# A running node older than this is considered orphaned by its worker.
# Bounds: below 5min a slow node would be stolen; above 2h duplicates the
# invocation-level orphan recovery horizon (3600s) too loosely.
STALE_RUNNING_TTL_S = 1800


class ResumeAction:
    skip = "skip"
    leave_running = "leave_running"
    retry = "retry"
    recover = "recover"
    manual = "manual"


@dataclass(frozen=True)
class ResumeDecision:
    action: str
    reason: str
    idempotency_key: str = ""
    requires_authorization: bool = False

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "requires_authorization": self.requires_authorization,
        }


def task_idempotency_key(plan_id: str, seq: int, name: str) -> str:
    """Deterministic, scope-safe dedupe key for one node's execution."""
    raw = f"{plan_id or ''}:{int(seq or 0)}:{(name or '').strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def is_stale(started_at: datetime | None, *, now: datetime | None = None,
             ttl_s: int = STALE_RUNNING_TTL_S) -> bool:
    """True when a running item has exceeded the stale TTL. Naive datetimes
    are treated as UTC (SQLite rows); None is never claimed stale."""
    if started_at is None:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    return (ref - started_at).total_seconds() > ttl_s


_TRANSIENT_MARKERS = (
    "timeout", "timed out", "connection", "refused", "reset", "unreachable",
    "rate limit", "429", "temporar", "deadlock", "lock wait", "超时", "连接",
    "暂时",
)
_TERMINAL_MARKERS = (
    "permission", "not authorized", "unauthorized", "access denied",
    "policy denied", "approval", "权限", "拒绝", "未授权",
)


def resume_decision(
    *,
    status: str,
    started_at: datetime | None = None,
    error: str = "",
    mutating: bool = False,
    requires_confirmation: bool = False,
    idempotency_key: str = "",
    plan_id: str = "",
    seq: int = 0,
    name: str = "",
    now: datetime | None = None,
) -> ResumeDecision:
    """Decide what may safely happen to one durable task after a restart.

    Safety precedence: terminal-state consistency > approval/authorization >
    duplicate prevention > transient recovery.
    """
    key = idempotency_key or task_idempotency_key(plan_id, seq, name)
    err_low = (error or "").lower()

    # 1. Terminal states are never re-touched (duplicate prevention).
    if status in ("completed", "skipped"):
        return ResumeDecision(
            ResumeAction.skip,
            "Already terminal — no duplicate execution",
            idempotency_key=key,
        )

    # 2. Approval-gated work: recovery NEVER bypasses approval.
    if requires_confirmation:
        return ResumeDecision(
            ResumeAction.manual,
            "Approval-gated work — recovery must not bypass human approval",
            idempotency_key=key,
            requires_authorization=True,
        )

    # 3. Authorization failures never auto-resume.
    if status == "failed" and any(m in err_low for m in _TERMINAL_MARKERS):
        return ResumeDecision(
            ResumeAction.manual,
            "Authorization boundary — never rerouted automatically",
            idempotency_key=key,
            requires_authorization=True,
        )

    if status == "running":
        if not is_stale(started_at, now=now):
            return ResumeDecision(
                ResumeAction.leave_running,
                "A live worker owns this task (within the stale TTL)",
                idempotency_key=key,
            )
        # Stale: the worker is gone. Consequential writes must not be
        # re-driven (outcome unknown — the write may have landed).
        if mutating:
            return ResumeDecision(
                ResumeAction.manual,
                "Stale running node with consequential write — verify the "
                "resource state first (read-only check); never auto-rerun",
                idempotency_key=key,
                requires_authorization=True,
            )
        return ResumeDecision(
            ResumeAction.recover,
            "Stale running read-only node — safe to re-own and re-execute "
            "under its idempotency key",
            idempotency_key=key,
        )

    if status == "failed":
        transient = any(m in err_low for m in _TRANSIENT_MARKERS)
        if transient:
            return ResumeDecision(
                ResumeAction.retry,
                "Transient failure — retry is eligible within the healing "
                "budget",
                idempotency_key=key,
            )
        return ResumeDecision(
            ResumeAction.manual,
            "Non-transient failure — inspect before rerunning",
            idempotency_key=key,
        )

    # pending / approved / unknown statuses: safe to start (fresh execution).
    return ResumeDecision(
        ResumeAction.retry,
        "Pending work — start under its idempotency key",
        idempotency_key=key,
    )
