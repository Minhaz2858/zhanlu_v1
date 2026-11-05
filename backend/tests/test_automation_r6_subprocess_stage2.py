"""R6 Phase 4 — dispatcher stage-2 subprocess wiring (flag-gated).

When ``AUTOMATION_SUBPROCESS_MODE`` is ON, ``_run_executor`` routes the run
through ``_run_subprocess_stage2``: child runs the harness over the §6.5
JSONL pipe, the PARENT finalizes fenced (child never writes state). Flag
OFF (default) keeps the in-proc executor — verified by all existing suites.
"""
from __future__ import annotations

import os
import sys
import uuid

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402

import app.services.automation_dispatcher as disp  # noqa: E402
# _run_subprocess_stage2 opens app.database.SessionLocal — use the shared
# conftest DB (same pattern as the executor-fencing tests).
from app.database import SessionLocal  # noqa: E402
from app.models.automation_execution import AutomationExecution  # noqa: E402
from app.models.automation_task import AutomationTask  # noqa: E402


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _run(s):
    t = AutomationTask(name=f"S2 {uuid.uuid4().hex[:6]}", type="report",
                       prompt="p", status="active")
    s.add(t)
    s.commit()
    ex = AutomationExecution(automation_task_id=t.id, status="running",
                             attempt=0)
    s.add(ex)
    s.commit()
    return t, ex


def _own_lease(execution_id):
    from app.services.automation.scheduler_core import acquire_lease
    s = SessionLocal()
    try:
        return acquire_lease(s, execution_id, disp._worker_id())
    finally:
        s.commit()
        s.close()


def test_stage2_flag_off_uses_inproc_executor(db, monkeypatch):
    """Default: AUTOMATION_SUBPROCESS_MODE absent → in-proc path untouched."""
    called = []
    import app.services.automation_executor as aexec
    monkeypatch.setattr(aexec, "execute_automation",
                        lambda eid: called.append(eid))
    import asyncio
    asyncio.run(disp._run_executor("ex-x"))
    assert called == ["ex-x"]  # in-proc executor used, no subprocess


def test_stage2_finalize_run_finished_fenced(db, monkeypatch):
    """Parent-owned finalization: run_finished terminal from the child ⇒
    output persisted + fenced succeeded on the execution row."""
    s = db
    t, ex = _run(s)
    _own_lease(ex.id)

    summary = {
        "outcome": "completed", "returncode": 0,
        "critical_count": 2, "telemetry_count": 3, "telemetry_gaps": 0,
        "dropped_telemetry": 0, "partial_frames": 0,
        "protocol_violation": False,
        "terminal_event": {"type": "run_finished",
                           "payload": {"status": "succeeded",
                                       "content": "the final report"}},
    }
    monkeypatch.setattr(
        "app.services.automation.subprocess_runner.launch_subprocess_runner",
        lambda job, **kw: summary)
    disp._run_subprocess_stage2(ex.id)

    s.expire_all()
    row = s.get(AutomationExecution, ex.id)
    assert row.status == "succeeded"
    assert row.output_text == "the final report"


def test_stage2_finalize_run_failed_keeps_partial(db, monkeypatch):
    s = db
    t, ex = _run(s)
    _own_lease(ex.id)
    summary = {
        "outcome": "completed", "returncode": 1,
        "critical_count": 1, "telemetry_count": 1, "telemetry_gaps": 0,
        "dropped_telemetry": 0, "partial_frames": 0,
        "protocol_violation": False,
        "terminal_event": {"type": "run_failed",
                           "payload": {"classification": "error",
                                       "error": "provider 503",
                                       "content": "partial answer"}},
    }
    monkeypatch.setattr(
        "app.services.automation.subprocess_runner.launch_subprocess_runner",
        lambda job, **kw: summary)
    disp._run_subprocess_stage2(ex.id)

    s.expire_all()
    row = s.get(AutomationExecution, ex.id)
    assert row.status == "failed"
    assert "provider 503" in (row.error or "")
    assert row.output_text == "partial answer"  # partial preserved


def test_stage2_crash_maps_to_failed_transient(db, monkeypatch):
    """Child crash (EOF, no terminal) ⇒ fenced failed(transient) per §6.5."""
    s = db
    t, ex = _run(s)
    _own_lease(ex.id)
    summary = {
        "outcome": "crashed", "returncode": -9,
        "critical_count": 0, "telemetry_count": 1, "telemetry_gaps": 0,
        "dropped_telemetry": 0, "partial_frames": 1,
        "protocol_violation": False, "terminal_event": None,
    }
    monkeypatch.setattr(
        "app.services.automation.subprocess_runner.launch_subprocess_runner",
        lambda job, **kw: summary)
    disp._run_subprocess_stage2(ex.id)

    s.expire_all()
    row = s.get(AutomationExecution, ex.id)
    assert row.status == "failed"
    assert "subprocess crashed" in (row.error or "")


def test_stage2_no_ownable_lease_leaves_row_for_recovery(db, monkeypatch):
    """A foreign live lease ⇒ the parent must NOT finalize (stale worker
    rule §6.2b) — the row is left untouched for recovery."""
    s = db
    t, ex = _run(s)
    from app.services.automation.scheduler_core import acquire_lease
    s2 = SessionLocal()
    acquire_lease(s2, ex.id, "another-worker")
    s2.commit()
    s2.close()
    summary = {
        "outcome": "completed", "returncode": 0,
        "critical_count": 1, "telemetry_count": 0, "telemetry_gaps": 0,
        "dropped_telemetry": 0, "partial_frames": 0,
        "protocol_violation": False,
        "terminal_event": {"type": "run_finished",
                           "payload": {"status": "succeeded"}},
    }
    monkeypatch.setattr(
        "app.services.automation.subprocess_runner.launch_subprocess_runner",
        lambda job, **kw: summary)
    disp._run_subprocess_stage2(ex.id)

    s.expire_all()
    row = s.get(AutomationExecution, ex.id)
    assert row.status == "running"  # untouched — recovery owns it
