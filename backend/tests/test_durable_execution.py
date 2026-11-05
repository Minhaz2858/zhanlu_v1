"""Phase 3.1 — durable execution & task recovery (TDD).

Reuse map (audit): PlanNode.result + ObservationRecord are ALREADY durable;
checkpoints exist (AgentRunStep.is_checkpoint); orphan recovery is scheduled.
MISSING: a resume/eligibility decision layer (what may safely resume after a
restart), deterministic idempotency keys (no duplicate execution), stale-task
detection, evidence references on ObservationRecord, and recovery-attempt
persistence shape.

A resumed task must NEVER bypass permissions or approvals; consequential
writes are never automatically repeated.

Run: pytest tests/test_durable_execution.py -q
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.durable_execution import (
    ResumeAction,
    resume_decision,
    task_idempotency_key,
    is_stale,
    STALE_RUNNING_TTL_S,
)


def _ts(seconds_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


# ── Node-level resume decisions ──────────────────────────────────────────────
def test_completed_node_is_never_reexecuted():
    d = resume_decision(status="completed", started_at=_ts(7200))
    assert d.action == ResumeAction.skip
    assert "no duplicate" in d.reason.lower() or "already" in d.reason.lower()


def test_skipped_node_stays_skipped():
    assert resume_decision(status="skipped", started_at=_ts(10)).action == ResumeAction.skip


def test_failed_transient_node_is_retry_eligible():
    d = resume_decision(status="failed", started_at=_ts(600),
                        error="connection refused")
    assert d.action == ResumeAction.retry
    assert d.idempotency_key  # dedupe guard for the retry


def test_failed_terminal_node_never_auto_resumes():
    d = resume_decision(status="failed", started_at=_ts(600),
                        error="Permission denied: table finances")
    assert d.action == ResumeAction.manual
    assert d.requires_authorization is True


def test_running_node_within_ttl_left_alone():
    d = resume_decision(status="running", started_at=_ts(60))
    assert d.action == ResumeAction.leave_running


def test_stale_running_read_only_node_is_recoverable():
    d = resume_decision(status="running", started_at=_ts(STALE_RUNNING_TTL_S + 120),
                        mutating=False)
    assert d.action == ResumeAction.recover
    assert d.idempotency_key


def test_stale_running_MUTATING_node_never_auto_rerun():
    d = resume_decision(status="running", started_at=_ts(STALE_RUNNING_TTL_S + 120),
                        mutating=True)
    assert d.action == ResumeAction.manual
    assert "write" in d.reason.lower() or "consequential" in d.reason.lower()


def test_approval_pending_node_requires_human_never_resume():
    d = resume_decision(status="running", started_at=_ts(STALE_RUNNING_TTL_S + 999),
                        requires_confirmation=True)
    assert d.action == ResumeAction.manual
    assert d.requires_authorization is True
    assert "approval" in d.reason.lower()


# ── Idempotency keys ─────────────────────────────────────────────────────────
def test_idempotency_key_deterministic_and_scoped():
    k1 = task_idempotency_key("plan-1", 3, "inventory check")
    k2 = task_idempotency_key("plan-1", 3, "inventory check")
    k3 = task_idempotency_key("plan-1", 4, "inventory check")
    k4 = task_idempotency_key("plan-2", 3, "inventory check")
    assert k1 == k2
    assert k1 != k3 and k1 != k4
    assert len(k1) <= 64  # storable


# ── Stale detection ──────────────────────────────────────────────────────────
def test_is_stale_boundaries():
    assert is_stale(_ts(STALE_RUNNING_TTL_S + 1)) is True
    assert is_stale(_ts(30)) is False
    assert is_stale(None) is False  # no timestamp → not claimed stale


def test_stale_ttl_is_configured_sane():
    assert 300 <= STALE_RUNNING_TTL_S <= 7200  # between 5min and 2h


# ── Evidence references persisted on ObservationRecord (migration 115) ──────
def test_observation_record_has_evidence_ids_column():
    from app.models.execution import ObservationRecord

    assert hasattr(ObservationRecord, "evidence_ids")


def test_migration_115_exists_and_is_guarded():
    import glob
    import os

    matches = glob.glob(
        os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "115_*")
    )
    assert matches, "migration 115 (observation evidence_ids) missing"
    src = open(matches[0]).read()
    # information_schema guard (deploy-safe pattern used since V050)
    assert "information_schema" in src
