"""R6 Phase 1 — executor fencing migration (TS §6.2/§6.2b).

The executor is the lease owner in-process (the dispatcher acquired the
lease under the same worker id before spawning it), so every worker-owned
state transition resolves its token via the live lease row and writes
through the runner protocol. Flag ``AUTOMATION_RUNNER_PROTOCOL`` (env,
default on) keeps the legacy plain-CAS behavior available for rollback.

NOT worker-owned (deliberately unfenced, documented):
- ``_mark_cancelled`` — user-plane action; CAS racing semantics with
  terminal-wins (§3.2).
- janitor reap — recovery plane operating on dead/hung runs.
- progress/telemetry writer — bounded-loss telemetry (Option C).
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

# The executor opens its OWN SessionLocal — use the shared conftest DB.
from app.database import SessionLocal  # noqa: E402
from app.models.automation_execution import AutomationExecution  # noqa: E402
from app.models.automation_scheduling import ExecutionLease  # noqa: E402
from app.models.automation_task import AutomationTask  # noqa: E402
from app.services.automation import runner_protocol as rp  # noqa: E402
from app.services.automation.scheduler_core import acquire_lease  # noqa: E402


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _task(db):
    t = AutomationTask(name=f"R6F {uuid.uuid4().hex[:6]}", type="report", prompt="p",
                       status="active")
    db.add(t)
    db.commit()
    return t


def _exec(db, task, status="queued", **kw):
    ex = AutomationExecution(automation_task_id=task.id, status=status, attempt=0, **kw)
    db.add(ex)
    db.commit()
    return ex


def _own_lease(db, ex):
    """The dispatcher's in-process lease (same worker id as the executor)."""
    from app.services.automation_dispatcher import _worker_id

    return acquire_lease(db, ex.id, _worker_id())


# ---------------------------------------------------------------------------
# current_token resolution
# ---------------------------------------------------------------------------


def test_current_token_resolves_own_live_lease(db):
    from app.services.automation_dispatcher import _worker_id

    t = _task(db)
    ex = _exec(db, t, status="running")
    token = _own_lease(db, ex)
    assert rp.current_token(db, ex.id, _worker_id()) == token


def test_current_token_refuses_foreign_live_lease(db):
    t = _task(db)
    ex = _exec(db, t, status="running")
    acquire_lease(db, ex.id, "some-other-worker")
    assert rp.current_token(db, ex.id, "yet-another-worker") is None


def test_current_token_refuses_expired_lease(db):
    from app.services.automation_dispatcher import _worker_id

    t = _task(db)
    ex = _exec(db, t, status="running")
    _own_lease(db, ex)
    lease = db.get(ExecutionLease, ex.id)
    lease.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    assert rp.current_token(db, ex.id, _worker_id()) is None


# ---------------------------------------------------------------------------
# §6.2b two-step finalization through the executor path
# ---------------------------------------------------------------------------


def test_executor_finalization_two_step_with_output_first(db):
    """running → (output already on row) → finalizing → terminal, fenced."""
    from app.services.automation_dispatcher import _worker_id

    t = _task(db)
    ex = _exec(db, t, status="running")
    token = _own_lease(db, ex)
    ex.output_text = "final report text"
    db.commit()

    ok = rp.finalize_from_running(
        db, ex.id, token, AutomationExecution,
        terminal_status="succeeded", duration_seconds=8.0,
    )
    assert ok is True
    db.expire_all()
    row = db.query(AutomationExecution).get(ex.id)
    assert row.status == "succeeded"
    assert row.output_text == "final report text"
    assert row.duration_seconds == 8.0
    # Lease released after terminal.
    assert db.get(ExecutionLease, ex.id).lease_expires_at <= datetime.utcnow()


def test_executor_finalization_stale_token_discards_result(db):
    t = _task(db)
    ex = _exec(db, t, status="running")
    stale = _own_lease(db, ex)
    ex.output_text = "stale worker's report"
    db.commit()
    # Recovery took over the lease (worker was presumed dead).
    lease = db.get(ExecutionLease, ex.id)
    lease.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    fresh = acquire_lease(db, ex.id, "recovery-worker")

    ok = rp.finalize_from_running(
        db, ex.id, stale, AutomationExecution, terminal_status="succeeded",
    )
    assert ok is False
    db.expire_all()
    row = db.query(AutomationExecution).get(ex.id)
    # Recovery owns the row — the stale worker's terminal never landed.
    assert row.status == "running"


def test_mark_failed_no_retry_fenced_under_flag(db, monkeypatch):
    from app.services.automation_executor import _mark_failed_no_retry

    t = _task(db)
    ex = _exec(db, t, status="running")
    _own_lease(db, ex)
    _mark_failed_no_retry(db, ex, "paused for confirmation")
    db.expire_all()
    assert db.query(AutomationExecution).get(ex.id).status == "failed"


def test_mark_failed_no_retry_foreign_lease_blocks_worker_write(db, monkeypatch):
    """A worker whose lease was taken over must NOT terminalize the row."""
    from app.services.automation_executor import _mark_failed_no_retry

    t = _task(db)
    ex = _exec(db, t, status="running")
    acquire_lease(db, ex.id, "recovery-worker")  # foreign owner
    _mark_failed_no_retry(db, ex, "stale worker failure")
    db.expire_all()
    assert db.query(AutomationExecution).get(ex.id).status == "running"


def test_mark_cancelled_works_without_lease(db):
    """Cancellation is a user-plane action — no lease required (§3.2)."""
    from app.services.automation_executor import _mark_cancelled

    t = _task(db)
    ex = _exec(db, t, status="running")
    assert _mark_cancelled(db, ex) is True
    db.expire_all()
    assert db.query(AutomationExecution).get(ex.id).status == "cancelled"


def test_mark_cancelled_terminal_wins_over_plane_cancel(db):
    t = _task(db)
    ex = _exec(db, t, status="succeeded")
    from app.services.automation_executor import _mark_cancelled

    assert _mark_cancelled(db, ex) is False  # terminal — immutable


# ---------------------------------------------------------------------------
# Legacy flag rollback
# ---------------------------------------------------------------------------


def test_legacy_flag_restores_plain_cas(db, monkeypatch):
    import app.services.automation_executor as aexec

    monkeypatch.setenv("AUTOMATION_RUNNER_PROTOCOL", "false")
    t = _task(db)
    ex = _exec(db, t, status="running")
    # No lease at all — legacy path writes anyway.
    from app.services.automation_executor import _mark_failed_no_retry

    _mark_failed_no_retry(db, ex, "legacy failure")
    db.expire_all()
    assert db.query(AutomationExecution).get(ex.id).status == "failed"
