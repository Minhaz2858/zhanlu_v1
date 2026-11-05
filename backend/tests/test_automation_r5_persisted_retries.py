"""R5 — persisted retries (in-memory ``_delayed_run`` retired) + cronjob
tool retirement.

Retry rows survive process death: the run-when lives in
``automation_executions.next_attempt_at`` and ANY dispatcher process
promotes them on its next tick.
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

# NOTE: schedule_retry/_promote_due_retries open their OWN SessionLocal, so
# these tests MUST run against the shared conftest database (the file-backed
# SQLite swapped into app.database) — a private in-memory engine would make
# the task invisible to the service (the known T1 gotcha).
from app.database import SessionLocal  # noqa: E402
from app.models.automation_execution import AutomationExecution  # noqa: E402
from app.models.automation_task import AutomationTask  # noqa: E402


@pytest.fixture
def db():
    s = SessionLocal()
    # The conftest DB truncates at MODULE boundaries, but queued retry rows
    # from earlier tests in this module would pollute promote queries —
    # clear them per test (leases first: FK). The rows under test are
    # re-created below.
    stale = s.query(AutomationExecution).filter(
        AutomationExecution.status == "queued",
        AutomationExecution.retry_of_execution_id.isnot(None),
    ).all()
    if stale:
        from app.models.automation_scheduling import (
            ExecutionLease,
            AutomationExecutionEvent,
            AutomationSideEffectOp,
        )
        stale_ids = [r.id for r in stale]
        for mdl in (ExecutionLease, AutomationExecutionEvent, AutomationSideEffectOp):
            s.query(mdl).filter(mdl.execution_id.in_(stale_ids)).delete(
                synchronize_session=False)
        s.query(AutomationExecution).filter(
            AutomationExecution.id.in_(stale_ids)).delete(synchronize_session=False)
    s.commit()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _task(db, max_retries="2"):
    t = AutomationTask(name=f"R5R {uuid.uuid4().hex[:6]}", type="report", prompt="p",
                       status="active", max_retries=max_retries)
    db.add(t)
    db.commit()
    return t


def _prev_execution(db, task, attempt=0):
    ex = AutomationExecution(automation_task_id=task.id, status="failed",
                             attempt=attempt, occurrence_id=f"occ-{uuid.uuid4().hex[:8]}")
    db.add(ex)
    db.commit()
    return ex


def test_schedule_retry_persists_row_with_backoff(db, monkeypatch):
    """No event loop needed — the retry row is the schedule."""
    import app.services.automation_dispatcher as disp

    # Simulate "no running main loop": the old code returned None and
    # created NOTHING (stranding the retry). The new code must still persist.
    monkeypatch.setattr(disp, "_main_loop", None)

    task = _task(db)
    prev = _prev_execution(db, task)
    exec_id = disp.schedule_retry(task.id, prev.id, prev.attempt, "boom")
    assert exec_id is not None  # persisted even without a loop
    row = db.query(AutomationExecution).get(exec_id)
    assert row.status == "queued"
    assert row.attempt == 1
    assert row.retry_of_execution_id == prev.id
    assert row.occurrence_id == prev.occurrence_id
    assert row.next_attempt_at is not None
    # Backoff window in the future (60s base).
    assert row.next_attempt_at > datetime.utcnow()


def test_schedule_retry_respects_max_retries(db):
    import app.services.automation_dispatcher as disp

    task = _task(db, max_retries="1")
    prev = _prev_execution(db, task, attempt=1)  # attempt 1 == max
    assert disp.schedule_retry(task.id, prev.id, prev.attempt, "boom") is None
    assert db.query(AutomationExecution).filter_by(
        automation_task_id=task.id, status="queued").count() == 0


def test_promote_due_retries_launches_only_due(db, monkeypatch):
    import asyncio

    import app.services.automation_dispatcher as disp

    task = _task(db)
    prev = _prev_execution(db, task)
    exec_id = disp.schedule_retry(task.id, prev.id, 0, "boom")

    launched = []
    captured = {}

    def _fake_launch(session, ex_id, occurrence=None, *, require_lease=False):
        launched.append(ex_id)
        captured["token"] = "tok-1"
        return "tok-1"

    monkeypatch.setattr(disp, "_launch_with_lease", _fake_launch)

    # Not yet due → nothing launched.
    now = datetime.now(timezone.utc)
    assert disp._promote_due_retries(db, now - timedelta(minutes=5)) == 0
    assert launched == []

    # Due → launched exactly once; the lease prevents re-promotion.
    assert disp._promote_due_retries(db, now + timedelta(minutes=5)) == 1
    assert launched == [exec_id]


def test_promoted_retry_carries_occurrence_and_attempt(db, monkeypatch):
    import app.services.automation_dispatcher as disp

    task = _task(db)
    prev = _prev_execution(db, task, attempt=0)
    exec_id = disp.schedule_retry(task.id, prev.id, 0, "boom")
    due = datetime.now(timezone.utc) + timedelta(minutes=5)
    monkeypatch.setattr(disp, "_launch_with_lease", lambda s, e, o=None, *, require_lease=False: "tok")
    disp._promote_due_retries(db, due)
    row = db.query(AutomationExecution).get(exec_id)
    assert row.attempt == 1
    assert row.occurrence_id == prev.occurrence_id  # same occurrence, next attempt


def test_retry_row_not_double_promoted_while_lease_live(db, monkeypatch):
    """A live lease on the retry row blocks a second promotion attempt —
    the multi-process safety property (PG-proven in the integration suite).
    Exercises the REAL launch path (only the executor is stubbed)."""
    import app.services.automation_dispatcher as disp

    async def _noop(execution_id):
        return None

    monkeypatch.setattr(disp, "_run_executor", _noop)

    task = _task(db)
    prev = _prev_execution(db, task)
    exec_id = disp.schedule_retry(task.id, prev.id, 0, "boom")
    # Simulate another worker already holding the promotion lease.
    from app.services.automation.scheduler_core import acquire_lease

    assert acquire_lease(db, exec_id, "other-worker") is not None
    due = datetime.now(timezone.utc) + timedelta(minutes=5)
    # _promote_due_retries must NOT launch (require_lease=True ⇒ None).
    assert disp._promote_due_retries(db, due) == 0
    db.expire_all()
    # The row stays queued — untouched, promotable after the lease expires.
    assert db.query(AutomationExecution).get(exec_id).status == "queued"


# ---------------------------------------------------------------------------
# cronjob tool retirement (roadmap R5: obsolete persistence path)
# ---------------------------------------------------------------------------


def test_cronjob_tool_retired():
    """The legacy cronjob tool must be gone from the registry and its
    persistence path (cronjobs.json) must no longer be imported."""
    import importlib

    # The module must no longer be importable via the tool_handlers package.
    import app.services.tool_handlers as th_pkg

    src = open(th_pkg.__file__).read()
    assert "cronjob_tool" not in src, (
        "tool_handlers/__init__.py still imports the retired cronjob_tool"
    )
    assert not os.path.exists(
        os.path.join(os.path.dirname(th_pkg.__file__), "cronjob_tool.py")
    ), "cronjob_tool.py must be deleted"


def test_legacy_cronjob_forbidden_lists_remain_harmless():
    """The tool name may stay in forbidden-lists of foreign-dirty files —
    forbidding a nonexistent tool is a no-op, and those files carry
    uncommitted owner work we must not touch."""
    from app.services.tool_registry import registry

    assert "create_cronjob" not in getattr(registry, "_tools", {})


# ---------------------------------------------------------------------------
# §6.3 non-destructive startup recovery (replaces the destructive force-reap)
# ---------------------------------------------------------------------------


def test_startup_recovery_finalize_replay_and_retry_branches(db, monkeypatch):
    import asyncio

    import app.services.automation_dispatcher as disp
    from app.models.automation_scheduling import AutomationOccurrence
    from app.services.automation.scheduler_core import acquire_lease

    async def _noop_sleep(_t):
        return None

    monkeypatch.setattr(disp, "TICK_INTERVAL", 9999)

    # Branch 1: output persisted + expired lease → finalize-replay (same row).
    t1 = _task(db)
    ex1 = AutomationExecution(automation_task_id=t1.id, status="finalizing",
                              attempt=0, output_text="persisted report",
                              occurrence_id=f"occ-{uuid.uuid4().hex[:8]}")
    db.add(ex1)
    db.commit()
    acquire_lease(db, ex1.id, "dead-worker", ttl_seconds=1)
    lease = db.query(
        __import__("app.models.automation_scheduling", fromlist=["ExecutionLease"]).ExecutionLease
    ).get(ex1.id)
    lease.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()

    # Branch 2: no output + expired lease + no side effects → new attempt.
    t2 = _task(db, max_retries="2")
    prev = AutomationExecution(automation_task_id=t2.id, status="running",
                               attempt=0, occurrence_id=f"occ-{uuid.uuid4().hex[:8]}")
    db.add(prev)
    db.commit()
    acquire_lease(db, prev.id, "dead-worker", ttl_seconds=1)
    from app.models.automation_scheduling import ExecutionLease

    db.get(ExecutionLease, prev.id).lease_expires_at = (
        datetime.utcnow() - timedelta(seconds=1))
    db.commit()

    # capture what startup recovery did
    calls = {"retried": 0, "finalized": 0}
    real_schedule = disp.schedule_retry

    def _spy_schedule(task_id, prev_id, attempt, error):
        calls["retried"] += 1
        return real_schedule(task_id, prev_id, attempt, error)

    monkeypatch.setattr(disp, "schedule_retry", _spy_schedule)

    asyncio.get_event_loop_policy()
    result = asyncio.new_event_loop().run_until_complete(disp.reap_on_startup())

    db.expire_all()
    row1 = db.query(AutomationExecution).get(ex1.id)
    assert row1.status in ("succeeded", "failed")  # finalized from output
    # Branch 2 produced a persisted retry row (same occurrence, attempt+1).
    retry_row = db.query(AutomationExecution).filter_by(
        retry_of_execution_id=prev.id).first()
    assert retry_row is not None
    assert retry_row.attempt == 1
    assert retry_row.occurrence_id == prev.occurrence_id
    assert result >= 1


def test_startup_recovery_live_lease_untouched(db):
    """§6.3: an unexpired lease owned elsewhere is NEVER touched (audit B4)."""
    import asyncio

    import app.services.automation_dispatcher as disp
    from app.services.automation.scheduler_core import acquire_lease

    t = _task(db)
    ex = AutomationExecution(automation_task_id=t.id, status="running", attempt=0)
    db.add(ex)
    db.commit()
    acquire_lease(db, ex.id, "another-live-process", ttl_seconds=600)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(disp.reap_on_startup())
    finally:
        loop.close()
    db.expire_all()
    assert db.query(AutomationExecution).get(ex.id).status == "running"
