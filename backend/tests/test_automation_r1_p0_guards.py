"""R1 P0 correctness hotfixes — regression tests (roadmap R1, audit B1/B7/B8/B10).

Covers the four approved hotfix areas:

1. (B1) Delete path stops writing ``status="deleted"`` — soft delete uses
   ``is_deleted=True`` only; ``status`` stays a canonical value.
2. (B10) Run-Now in-flight guard — ``trigger_now`` refuses when a
   non-terminal execution exists for the task (``AutomationRunConflict``);
   the router maps it to HTTP 409; ``force=True`` overrides and audits.
3. (B7) Artifact-render failure isolation — a render exception records
   ``artifacts_failed`` on the execution (output_data + activity step)
   without failing the run.
4. (B8) Delete cancels in-flight executions — queued/running executions
   of a deleted task are CAS'd to ``cancelled`` and the in-process cancel
   event is signalled; terminal history is preserved.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

# Hermetic in-memory SQLite; must be set BEFORE app.database import.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402
from app.models.automation_execution import AutomationExecution  # noqa: E402
from app.models.automation_task import AutomationTask  # noqa: E402
from app.models.audit_event import AuditEvent  # noqa: E402

_FAKE_USER = SimpleNamespace(id="r1-test-user", org_id=None)


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


def _make_task(d, **overrides) -> AutomationTask:
    defaults = dict(
        name=f"R1 Guard Task {uuid.uuid4().hex[:8]}",
        type="report",
        prompt="test prompt",
        description="test description",
        cron_expression="0 9 * * 1",
        project="Test Project",
        output_format="docx",
        is_deleted=False,
        notify_chat=False,
    )
    defaults.update(overrides)
    task = AutomationTask(**defaults)
    d.add(task)
    d.commit()
    d.refresh(task)
    return task


def _make_execution(d, task: AutomationTask, status: str = "running") -> AutomationExecution:
    ex = AutomationExecution(
        automation_task_id=task.id,
        status=status,
        attempt=0,
    )
    d.add(ex)
    d.commit()
    d.refresh(ex)
    return ex


async def _noop_run_executor(execution_id, *args, **kwargs):  # noqa: ARG001
    return None


@pytest.fixture
def no_executor(monkeypatch):
    """Neutralise the real executor spawn inside trigger_now."""
    monkeypatch.setattr(
        "app.services.automation_dispatcher._run_executor", _noop_run_executor
    )


# ---------------------------------------------------------------------------
# 1. (B1) Delete must not write status="deleted"
# ---------------------------------------------------------------------------


def test_delete_does_not_write_deleted_status(db):
    from app.routers.automation_api import delete_automation

    task = _make_task(db, status="paused")
    try:
        resp = delete_automation(task_id=task.id, db=db, user=_FAKE_USER)
        assert resp["success"] is True
        db.refresh(task)
        assert task.is_deleted is True
        # Status must remain canonical — never "deleted".
        assert task.status != "deleted"
        assert task.status in AutomationTask.VALID_STATUSES or task.status is None
    finally:
        task.is_deleted = True
        db.commit()


def test_delete_is_idempotent_second_call_404(db):
    from app.routers.automation_api import delete_automation

    task = _make_task(db, status="paused")
    delete_automation(task_id=task.id, db=db, user=_FAKE_USER)
    with pytest.raises(HTTPException) as exc:
        delete_automation(task_id=task.id, db=db, user=_FAKE_USER)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 2. (B10) Run-Now in-flight guard
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_trigger_now_conflict_when_inflight(no_executor):
    """trigger_now opens its own SessionLocal — use the shared conftest
    test DB (same pattern as test_automation_runtime_parity)."""
    from app.database import SessionLocal
    from app.services.automation_dispatcher import (
        AutomationRunConflict,
        trigger_now,
    )

    _UID = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        task = _make_task(db, name=f"R1 conflict {_UID}")
        _make_execution(db, task, status="queued")
        task_id = task.id
    finally:
        db.close()
    try:
        with pytest.raises(AutomationRunConflict):
            _run(trigger_now(task_id))
    finally:
        db = SessionLocal()
        try:
            for ex in db.query(AutomationExecution).filter(
                AutomationExecution.automation_task_id == task_id
            ).all():
                db.delete(ex)
            t = db.query(AutomationTask).filter(AutomationTask.id == task_id).first()
            if t:
                db.delete(t)
            db.commit()
        finally:
            db.close()


def test_trigger_now_force_overrides_conflict(no_executor):
    from app.database import SessionLocal
    from app.services.automation_dispatcher import trigger_now

    _UID = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        task = _make_task(db, name=f"R1 force {_UID}")
        _make_execution(db, task, status="running")
        task_id = task.id
    finally:
        db.close()
    exec_id = None
    try:
        exec_id = _run(trigger_now(task_id, force=True))
        assert exec_id
    finally:
        db = SessionLocal()
        try:
            for ex in db.query(AutomationExecution).filter(
                AutomationExecution.automation_task_id == task_id
            ).all():
                db.delete(ex)
            t = db.query(AutomationTask).filter(AutomationTask.id == task_id).first()
            if t:
                db.delete(t)
            db.commit()
        finally:
            db.close()


def test_trigger_now_ok_when_no_inflight(no_executor):
    from app.database import SessionLocal
    from app.services.automation_dispatcher import trigger_now

    _UID = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        task = _make_task(db, name=f"R1 ok {_UID}")
        task_id = task.id
    finally:
        db.close()
    try:
        exec_id = _run(trigger_now(task_id))
        assert exec_id
    finally:
        db = SessionLocal()
        try:
            for ex in db.query(AutomationExecution).filter(
                AutomationExecution.automation_task_id == task_id
            ).all():
                db.delete(ex)
            t = db.query(AutomationTask).filter(AutomationTask.id == task_id).first()
            if t:
                db.delete(t)
            db.commit()
        finally:
            db.close()


def test_run_now_endpoint_maps_conflict_to_409(db, no_executor, monkeypatch):
    from app.routers import automation_api

    task = _make_task(db)
    _make_execution(db, task, status="running")

    async def _conflict(task_id, parent_execution_id=None, force=False):  # noqa: ARG001
        from app.services.automation_dispatcher import AutomationRunConflict

        raise AutomationRunConflict("existing-exec-id")

    monkeypatch.setattr(automation_api, "trigger_now", _conflict, raising=False)
    monkeypatch.setattr(
        "app.services.automation_dispatcher.trigger_now", _conflict
    )
    with pytest.raises(HTTPException) as exc:
        _run(automation_api.run_now(task_id=task.id, db=db, user=_FAKE_USER))
    assert exc.value.status_code == 409
    assert exc.value.detail.get("code") == "already_running"
    assert exc.value.detail.get("execution_id") == "existing-exec-id"


def test_run_now_force_creates_audit_event(db, no_executor, monkeypatch):
    from app.routers import automation_api

    task = _make_task(db)
    _make_execution(db, task, status="running")

    async def _ok(task_id, parent_execution_id=None, force=False):  # noqa: ARG001
        return "exec-forced-1"

    monkeypatch.setattr(
        "app.services.automation_dispatcher.trigger_now", _ok
    )
    resp = _run(
        automation_api.run_now(
            task_id=task.id, db=db, user=_FAKE_USER, force=True
        )
    )
    assert resp["success"] is True
    audit = (
        db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "automation_run_forced",
            AuditEvent.resource_id == task.id,
        )
        .first()
    )
    assert audit is not None
    assert audit.actor_id == "r1-test-user"


# ---------------------------------------------------------------------------
# 3. (B7) Artifact-render failure isolation
# ---------------------------------------------------------------------------


def test_render_failure_recorded_and_run_not_failed(db):
    """The artifact-failure helper records honest failure info on the
    execution without changing run status."""
    from app.services.automation_executor import _record_artifact_render_failure

    task = _make_task(db)
    ex = _make_execution(db, task, status="running")
    ex.output_data = {"agent_id": "a1"}
    db.commit()
    _record_artifact_render_failure(
        db, ex, RuntimeError("boilerplate exploded")
    )
    db.refresh(ex)
    assert ex.status == "running"  # untouched by the helper
    assert ex.output_data.get("artifacts_failed") is True
    assert "boilerplate" in (ex.output_data.get("artifacts_failed_reason") or "")
    steps = ex.activity_steps or []
    assert any(
        isinstance(s, dict) and "artifact" in str(s.get("description", "")).lower()
        for s in steps
    )


def test_render_failure_helper_tolerates_none_output_data(db):
    from app.services.automation_executor import _record_artifact_render_failure

    task = _make_task(db)
    ex = _make_execution(db, task, status="running")
    try:
        _record_artifact_render_failure(db, ex, RuntimeError("x"))
        db.refresh(ex)
        assert ex.output_data.get("artifacts_failed") is True
    finally:
        pass


# ---------------------------------------------------------------------------
# 4. (B8) Delete cancels in-flight executions
# ---------------------------------------------------------------------------


def test_delete_cancels_queued_and_running_executions(db, monkeypatch):
    from app.routers.automation_api import delete_automation

    cancelled_signals: list[str] = []

    def _fake_request_cancel(execution_id: str) -> bool:
        cancelled_signals.append(execution_id)
        return True

    monkeypatch.setattr(
        "app.services.automation_executor.request_cancel", _fake_request_cancel
    )

    task = _make_task(db, status="active")
    running = _make_execution(db, task, status="running")
    queued = _make_execution(db, task, status="queued")
    done = _make_execution(db, task, status="completed")
    try:
        delete_automation(task_id=task.id, db=db, user=_FAKE_USER)
        db.refresh(running)
        db.refresh(queued)
        db.refresh(done)
        assert running.status == "cancelled"
        assert running.completed_at is not None
        assert queued.status == "cancelled"
        # Terminal history is preserved.
        assert done.status == "completed"
        # The in-process cancel event was signalled for the live run.
        assert running.id in cancelled_signals
    finally:
        db.refresh(task)
        task.is_deleted = True
        db.commit()


def test_delete_cancel_does_not_clobber_terminal_status(db, monkeypatch):
    """A racing terminal write wins over the delete-cancel CAS."""
    from app.routers.automation_api import delete_automation

    monkeypatch.setattr(
        "app.services.automation_executor.request_cancel",
        lambda execution_id: False,
    )
    task = _make_task(db, status="active")
    running = _make_execution(db, task, status="running")
    # Simulate the run finishing between our query and the CAS.
    running.status = "completed"
    running.completed_at = datetime.now(timezone.utc)
    db.commit()
    try:
        delete_automation(task_id=task.id, db=db, user=_FAKE_USER)
        db.refresh(running)
        assert running.status == "completed"
    finally:
        db.refresh(task)
        task.is_deleted = True
        db.commit()
