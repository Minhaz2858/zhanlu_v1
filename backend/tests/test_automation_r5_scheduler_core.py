"""R5 — scheduler core: occurrence claim, leases, fencing, F-27 barrier.

SQLite unit suite (mechanics). PostgreSQL concurrency/recovery guarantees
are proven separately in test_automation_r5_pg_integration.py (real PG,
skipped without a DSN — never faked on SQLite).
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402
from app.models.automation_execution import AutomationExecution  # noqa: E402
from app.models.automation_scheduling import (  # noqa: E402
    AutomationOccurrence,
    AutomationSideEffectOp,
    ExecutionLease,
)
from app.models.automation_task import AutomationTask  # noqa: E402
from app.services.automation import scheduler_core as sc  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _task(db):
    t = AutomationTask(name=f"R5 {uuid.uuid4().hex[:6]}", type="report", prompt="p",
                       status="active")
    db.add(t)
    db.commit()
    return t


# ---------------------------------------------------------------------------
# Occurrence claim arbiter
# ---------------------------------------------------------------------------


def test_claim_creates_and_claims(db):
    t = _task(db)
    occ = sc.claim_occurrence(
        db, automation_task_id=t.id, occurrence_key="scheduled:2026-11-03T08:00:00",
        scheduled_at=datetime(2026, 11, 3, 8, 0),
    )
    assert occ is not None and occ.status == "claimed"
    assert occ.occurrence_key == "scheduled:2026-11-03T08:00:00"


def test_double_claim_same_key_is_refused(db):
    t = _task(db)
    key = "scheduled:2026-11-03T08:00:00"
    first = sc.claim_occurrence(db, automation_task_id=t.id, occurrence_key=key,
                                scheduled_at=None)
    second = sc.claim_occurrence(db, automation_task_id=t.id, occurrence_key=key,
                                 scheduled_at=None)
    assert first is not None
    assert second is None  # claimed slot cannot be double-claimed


def test_claim_after_terminal_occurrence_is_refused(db):
    t = _task(db)
    key = "scheduled:2026-11-03T08:00:00"
    occ = sc.claim_occurrence(db, automation_task_id=t.id, occurrence_key=key,
                              scheduled_at=None)
    sc.complete_occurrence(db, occ, "completed")
    again = sc.claim_occurrence(db, automation_task_id=t.id, occurrence_key=key,
                                scheduled_at=None)
    assert again is None


def test_occurrence_keys_scoped_per_task(db):
    t1, t2 = _task(db), _task(db)
    key = "scheduled:2026-11-03T08:00:00"
    a = sc.claim_occurrence(db, automation_task_id=t1.id, occurrence_key=key,
                            scheduled_at=None)
    b = sc.claim_occurrence(db, automation_task_id=t2.id, occurrence_key=key,
                            scheduled_at=None)
    assert a is not None and b is not None  # same key, different tasks: legal


def test_complete_occurrence_rejects_nonterminal_status(db):
    t = _task(db)
    occ = sc.claim_occurrence(db, automation_task_id=t.id, occurrence_key="k1",
                              scheduled_at=None)
    with pytest.raises(ValueError):
        sc.complete_occurrence(db, occ, "pending")


# ---------------------------------------------------------------------------
# Leases + fencing
# ---------------------------------------------------------------------------


def test_lease_acquire_renew_release(db):
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    token = sc.acquire_lease(db, ex.id, "w1")
    assert token
    assert sc.renew_lease(db, ex.id, token) is True
    assert sc.release_lease(db, ex.id, token) is True


def test_live_lease_blocks_second_acquire(db):
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    assert sc.acquire_lease(db, ex.id, "w1") is not None
    assert sc.acquire_lease(db, ex.id, "w2") is None  # live lease held by w1


def test_expired_lease_is_takeoverable_with_new_token(db):
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    old_token = sc.acquire_lease(db, ex.id, "w1", ttl_seconds=1)
    # Force expiry.
    lease = db.get(ExecutionLease, ex.id)
    lease.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    new_token = sc.acquire_lease(db, ex.id, "w2")
    assert new_token and new_token != old_token
    # Old owner's fenced write loses; new owner's wins.
    assert sc.fenced_status_write(
        db, ex.id, old_token, from_statuses=("running",), to_status="succeeded",
        execution_cls=AutomationExecution,
    ) is False
    assert sc.fenced_status_write(
        db, ex.id, new_token, from_statuses=("running",), to_status="succeeded",
        execution_cls=AutomationExecution,
    ) is True


def test_fenced_write_requires_lease_token(db):
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    assert sc.fenced_status_write(
        db, ex.id, "no-such-token", from_statuses=("running",), to_status="failed",
        execution_cls=AutomationExecution,
    ) is False
    assert db.query(AutomationExecution).get(ex.id).status == "running"


# ---------------------------------------------------------------------------
# F-27 side-effect ledger + reconciliation barrier
# ---------------------------------------------------------------------------


def _op(db, ex, attempt, op_key, status="started", idempotent=False):
    op = AutomationSideEffectOp(
        execution_id=ex.id, attempt=attempt, op_key=op_key,
        tool_name="erp_submit", status=status, idempotent=idempotent,
        idempotency_key=f"{ex.id}:{attempt}:1",
    )
    db.add(op)
    db.commit()
    return op


def test_barrier_no_ledger_rows_is_unblocked(db):
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    result = sc.reconcile_side_effects(db, ex.id, 0)
    assert result["blocked"] is False


def test_barrier_provider_confirms_complete_never_repeats(db):
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    _op(db, ex, 0, "0:1:erp_submit")
    checks = []

    def provider_check(op):
        checks.append(op["op_key"])
        return "confirmed_complete"

    result = sc.reconcile_side_effects(db, ex.id, 0, provider_check=provider_check)
    assert result["blocked"] is False
    assert result["outcomes"]["0:1:erp_submit"] == "confirmed_complete"
    row = db.query(AutomationSideEffectOp).filter_by(op_key="0:1:erp_submit").first()
    assert row.reconciled_via == "provider_check"
    assert row.reconciled_at is not None


def test_barrier_unknown_non_idempotent_blocks_retry(db):
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    _op(db, ex, 0, "0:1:erp_submit", idempotent=False)  # provider_check absent
    result = sc.reconcile_side_effects(db, ex.id, 0, provider_check=None)
    assert result["blocked"] is True
    assert "0:1:erp_submit" in result["unresolved_non_idempotent"]


def test_barrier_unknown_idempotent_permits_retry(db):
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    _op(db, ex, 0, "0:1:erp_submit", idempotent=True)
    result = sc.reconcile_side_effects(db, ex.id, 0, provider_check=None)
    assert result["blocked"] is False
    assert result["outcomes"]["0:1:erp_submit"] == "unresolved"  # honestly unknown


def test_barrier_never_invents_reconciliation(db):
    """Provider returns garbage/None → outcome stays unresolved, not guessed."""
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    _op(db, ex, 0, "0:1:erp_submit", idempotent=True)
    result = sc.reconcile_side_effects(
        db, ex.id, 0, provider_check=lambda op: "definitely_fine_boss"
    )
    # Unrecognized verdict must NOT be persisted as a reconciliation.
    row = db.query(AutomationSideEffectOp).filter_by(op_key="0:1:erp_submit").first()
    assert row.reconciled_at is None
    assert result["outcomes"]["0:1:erp_submit"] == "unresolved"


def test_recover_blocked_writes_blocked_status(db):
    """Recovery with an unknown non-idempotent side effect → failed, no new attempt."""
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0,
                             occurrence_id="occ-1")
    db.add(ex)
    db.commit()
    sc.acquire_lease(db, ex.id, "w1", ttl_seconds=1)
    db.get(ExecutionLease, ex.id).lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    _op(db, ex, 0, "0:1:erp_submit", idempotent=False)

    attempts = []
    stats = sc.recover_expired_leases(
        db, execution_cls=AutomationExecution, owner="recoverer",
        new_attempt=lambda s, e: attempts.append(e.id),
    )
    assert stats["blocked"] == 1
    assert attempts == []  # automatic retry blocked
    db.expire_all()
    assert db.query(AutomationExecution).get(ex.id).status == "failed"
    assert "blocked_on_side_effect_reconciliation" in (
        db.query(AutomationExecution).get(ex.id).error or ""
    )


def test_recover_unexpired_lease_never_touched(db):
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    sc.acquire_lease(db, ex.id, "other-worker", ttl_seconds=600)
    stats = sc.recover_expired_leases(
        db, execution_cls=AutomationExecution, owner="recoverer",
    )
    assert stats["skipped_live"] == 0
    assert stats["retried"] == 0
    assert db.query(AutomationExecution).get(ex.id).status == "running"  # untouched


def test_recover_output_present_finalizes_same_execution(db):
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="finalizing", attempt=0,
                             output_text="the report")
    db.add(ex)
    db.commit()
    sc.acquire_lease(db, ex.id, "w1", ttl_seconds=1)
    db.get(ExecutionLease, ex.id).lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    finalized = []
    stats = sc.recover_expired_leases(
        db, execution_cls=AutomationExecution, owner="recoverer",
        finalize_replay=lambda s, e: (finalized.append(e.id), True)[1],
    )
    assert stats["finalized"] == 1
    assert finalized == [ex.id]


def test_barrier_timeout_ageout_marks_unknown_and_blocks_non_idempotent(db):
    """F-27 (c): no provider check available + row older than the window ⇒
    ``unknown`` via ``timeout_ageout``; unknown + non-idempotent BLOCKS the
    automatic retry (never guessed, never silently re-run)."""
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    op = _op(db, ex, 0, "0:1:erp_submit", idempotent=False)
    # Age the ledger row beyond the (shortened) reconciliation window.
    op.updated_date = datetime.utcnow() - timedelta(hours=25)
    db.commit()
    result = sc.reconcile_side_effects(
        db, ex.id, 0, provider_check=None, reconciliation_window_seconds=3600,
    )
    row = db.query(AutomationSideEffectOp).filter_by(op_key="0:1:erp_submit").first()
    assert row.status == "unknown"
    assert row.reconciled_via == "timeout_ageout"
    assert row.reconciled_at is not None
    assert result["blocked"] is True
    assert "0:1:erp_submit" in result["unresolved_non_idempotent"]


def test_barrier_timeout_ageout_idempotent_permits_retry(db):
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    op = _op(db, ex, 0, "0:1:db_write", idempotent=True)
    op.updated_date = datetime.utcnow() - timedelta(hours=25)
    db.commit()
    result = sc.reconcile_side_effects(
        db, ex.id, 0, provider_check=None, reconciliation_window_seconds=3600,
    )
    row = db.query(AutomationSideEffectOp).filter_by(op_key="0:1:db_write").first()
    assert row.status == "unknown"
    assert row.reconciled_via == "timeout_ageout"
    assert result["blocked"] is False  # unknown + idempotent ⇒ retry allowed


def test_barrier_provider_check_overrides_ageout(db):
    """A decisive provider check wins even when the row is stale."""
    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    op = _op(db, ex, 0, "0:1:erp_submit", idempotent=False)
    op.updated_date = datetime.utcnow() - timedelta(hours=25)
    db.commit()
    result = sc.reconcile_side_effects(
        db, ex.id, 0, provider_check=lambda o: "confirmed_not_started",
        reconciliation_window_seconds=3600,
    )
    row = db.query(AutomationSideEffectOp).filter_by(op_key="0:1:erp_submit").first()
    assert row.status == "confirmed_not_started"
    assert row.reconciled_via == "provider_check"
    assert result["blocked"] is False  # confirmed not started ⇒ safe retry
