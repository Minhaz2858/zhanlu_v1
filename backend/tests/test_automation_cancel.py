"""Cancel infrastructure tests for automation executions.

Covers:

1. ``request_cancel`` / ``_register_cancel_event`` / ``_clear_cancel_event``
   form a small, thread-safe registry keyed by execution id. Setting the
   event on a registered id is observable from another thread (this is
   the whole reason it's a threading.Event instead of an asyncio.Event).

2. ``_mark_cancelled`` is a CAS that only flips queued|running -> cancelled.
   Calling it on an already-cancelled row is a no-op (rowcount=0).
   Calling it on a succeeded/failed row is also a no-op (we never want
   to silently clobber a terminal success/failure).

3. The cancel HTTP endpoint ``POST /api/automations/executions/{id}/cancel``
   is idempotent: cancelling twice returns ``already_terminal=True`` on
   the second call and never raises. 404s on unknown ids.

4. ``_persist_cancellation_to_chat`` writes a "⏹ Run cancelled by user"
   line into the pre-existing empty assistant bubble that
   ``_post_run_request_marker`` created for the same execution. If the
   marker never ran (e.g. fast-fail), it appends a fresh assistant
   message instead. Never raises.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

# Use in-memory SQLite so the test is hermetic. ``app.database`` reads
# DATABASE_URL at import time, so this must be set BEFORE the import.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.automation_execution import AutomationExecution
from app.models.automation_task import AutomationTask
from app.models.chat_message import ChatMessage


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory SQLite with all tables created via the full
    model registry — same code path the real backend uses."""
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
        name="Cancel Test Task",
        type="report",
        prompt="test prompt",
        description="test description",
        cron_expression="0 9 * * 1",
        project="Test Project",
        output_format="docx",
        is_deleted=False,
        notify_chat=True,
    )
    defaults.update(overrides)
    task = AutomationTask(**defaults)
    d.add(task)
    d.commit()
    d.refresh(task)
    return task


def _make_execution(d, task: AutomationTask, status: str = "running", attempt: int = 0) -> AutomationExecution:
    ex = AutomationExecution(
        automation_task_id=task.id,
        status=status,
        attempt=attempt,
    )
    d.add(ex)
    d.commit()
    d.refresh(ex)
    return ex


def _teardown(d, task: AutomationTask, execution: AutomationExecution) -> None:
    """FK-safe teardown for cancel tests."""
    sid = task.session_id if task else None
    if sid:
        for m in d.query(ChatMessage).filter(ChatMessage.session_id == sid).all():
            try:
                d.delete(m)
            except Exception:
                pass
    d.commit()
    if execution is not None:
        try:
            d.refresh(execution)
        except Exception:
            pass
        try:
            d.delete(execution)
        except Exception:
            pass
    d.commit()
    if task is not None:
        try:
            task.session_id = None
            d.commit()
            d.refresh(task)
        except Exception:
            pass
        try:
            d.delete(task)
        except Exception:
            pass
        d.commit()
    if sid:
        from app.models.agent_conversation import AgentConversation
        from app.models.chat_session import ChatSession
        sess = d.query(ChatSession).filter(ChatSession.id == sid).first()
        conv_id = sess.conversation_id if sess else None
        if sess:
            try:
                d.delete(sess)
                d.commit()
            except Exception:
                pass
        if conv_id:
            conv = d.query(AgentConversation).filter(
                AgentConversation.id == conv_id,
            ).first()
            if conv:
                try:
                    d.delete(conv)
                    d.commit()
                except Exception:
                    pass


# --- Test 1: cancel-event registry -----------------------------------------


def test_request_cancel_round_trip():
    """Register → set → clear. The set() must be observable from another
    thread (this is the whole reason it's a threading.Event instead of
    an asyncio.Event)."""
    from app.services.automation_executor import (
        _register_cancel_event,
        _clear_cancel_event,
        request_cancel,
    )
    eid = f"test-exe-{uuid.uuid4().hex[:8]}"
    try:
        ev = _register_cancel_event(eid)
        assert ev is not None
        assert ev.is_set() is False, "fresh event must be unset"

        # Setting from another thread must be visible immediately.
        observed = []
        def waiter():
            for _ in range(50):
                if ev.is_set():
                    observed.append(True)
                    return
                time.sleep(0.01)

        t = threading.Thread(target=waiter)
        t.start()
        delivered = request_cancel(eid)
        t.join(timeout=2.0)
        assert delivered is True, "request_cancel must report True for live event"
        assert observed == [True], "waiter thread must observe the set()"
        assert ev.is_set() is True
    finally:
        _clear_cancel_event(eid)
    # Cleared slot is gone — a new request_cancel returns False.
    assert request_cancel(eid) is False


def test_request_cancel_unknown_id_returns_false():
    from app.services.automation_executor import request_cancel
    assert request_cancel(f"never-registered-{uuid.uuid4().hex}") is False


def test_register_resets_stale_event():
    """A re-register must clear the event, not inherit a stale set()."""
    from app.services.automation_executor import (
        _register_cancel_event,
        _clear_cancel_event,
        request_cancel,
    )
    eid = f"test-exe-{uuid.uuid4().hex[:8]}"
    try:
        ev1 = _register_cancel_event(eid)
        request_cancel(eid)
        assert ev1.is_set() is True
        # A retry (e.g. executor re-launched) registers a fresh event.
        ev2 = _register_cancel_event(eid)
        assert ev2 is ev1, "register is idempotent on the same id"
        assert ev2.is_set() is False, "re-register must clear stale set()"
    finally:
        _clear_cancel_event(eid)


# --- Test 2: _mark_cancelled CAS -------------------------------------------


def test_mark_cancelled_flips_running_row(db):
    task = _make_task(db, name="Cancel CAS Test 1")
    ex = _make_execution(db, task, status="running")
    try:
        from app.services.automation_executor import _mark_cancelled
        flipped = _mark_cancelled(db, ex)
        assert flipped is True, "queued|running -> cancelled should flip"
        db.refresh(ex)
        assert ex.status == "cancelled"
        assert ex.error == "Cancelled by user"
        assert ex.completed_at is not None
    finally:
        _teardown(db, task, ex)


def test_mark_cancelled_is_idempotent(db):
    task = _make_task(db, name="Cancel CAS Test 2")
    ex = _make_execution(db, task, status="running")
    try:
        from app.services.automation_executor import _mark_cancelled
        assert _mark_cancelled(db, ex) is True
        # Second call: row is already "cancelled" → CAS misses.
        assert _mark_cancelled(db, ex) is False, "second call must be a no-op"
    finally:
        _teardown(db, task, ex)


def test_mark_cancelled_refuses_to_clobber_succeeded(db):
    """NEVER silently overwrite a terminal non-cancelled status. The
    user might already be looking at the success page when the cancel
    button is hit; flipping to "cancelled" would lose the result."""
    task = _make_task(db, name="Cancel CAS Test 3")
    ex = _make_execution(db, task, status="succeeded")
    try:
        from app.services.automation_executor import _mark_cancelled
        flipped = _mark_cancelled(db, ex)
        assert flipped is False, "succeeded must NOT be flipped"
        db.refresh(ex)
        assert ex.status == "succeeded", "succeeded must be preserved"
    finally:
        _teardown(db, task, ex)


# --- Test 3: cancel HTTP endpoint ------------------------------------------


def test_cancel_endpoint_flips_running_execution(db):
    task = _make_task(db, name="Cancel Endpoint Test 1")
    ex = _make_execution(db, task, status="running")
    try:
        from fastapi import HTTPException
        from app.routers.automation_api import cancel_execution_endpoint
        resp = cancel_execution_endpoint(execution_id=ex.id, db=db)
        assert resp["status"] == "cancelled"
        assert resp["already_terminal"] is False
        # request_cancel returns False because no executor is registered
        # in this test — that's fine, the DB flip is authoritative.
        assert resp["delivered"] is False
        db.refresh(ex)
        assert ex.status == "cancelled"
    finally:
        _teardown(db, task, ex)


def test_cancel_endpoint_is_idempotent(db):
    task = _make_task(db, name="Cancel Endpoint Test 2")
    ex = _make_execution(db, task, status="running")
    try:
        from app.routers.automation_api import cancel_execution_endpoint
        r1 = cancel_execution_endpoint(execution_id=ex.id, db=db)
        r2 = cancel_execution_endpoint(execution_id=ex.id, db=db)
        assert r1["status"] == "cancelled"
        # Second call sees the row already in "cancelled" → short-circuit
        # before the CAS, return already_terminal=True.
        assert r2["status"] == "cancelled"
        assert r2["already_terminal"] is True
    finally:
        _teardown(db, task, ex)


def test_cancel_endpoint_404_for_unknown_execution(db):
    from fastapi import HTTPException
    from app.routers.automation_api import cancel_execution_endpoint
    try:
        cancel_execution_endpoint(execution_id="nope-does-not-exist", db=db)
    except HTTPException as e:
        assert e.status_code == 404
        return
    raise AssertionError("expected 404 for unknown execution")


# --- Test 4: _persist_cancellation_to_chat ---------------------------------


def test_persist_cancellation_updates_existing_assistant_bubble(db):
    """When ``_post_run_request_marker`` already created the empty
    assistant bubble for the same execution, the cancel helper updates
    it in place (no duplicate row)."""
    task = _make_task(db, name="Cancel Chat Test 1")
    ex = _make_execution(db, task, status="running")
    try:
        from app.services.automation_executor import (
            _post_run_request_marker,
            _persist_cancellation_to_chat,
        )
        _post_run_request_marker(db, task, ex, trigger="run")
        # Confirm the marker created the empty assistant bubble.
        asst = [
            m for m in db.query(ChatMessage).filter(
                ChatMessage.session_id == task.session_id,
            ).all() if m.role == "assistant"
        ]
        assert len(asst) == 1
        assert asst[0].content == ""

        _persist_cancellation_to_chat(db, task, ex)

        asst_after = [
            m for m in db.query(ChatMessage).filter(
                ChatMessage.session_id == task.session_id,
            ).all() if m.role == "assistant"
        ]
        assert len(asst_after) == 1, "no duplicate row"
        assert "Run cancelled by user" in asst_after[0].content
        assert asst_after[0].phase.get("status") == "cancelled"
        assert asst_after[0].phase.get("execution_id") == ex.id
    finally:
        _teardown(db, task, ex)


def test_persist_cancellation_appends_when_no_marker(db):
    """If the marker never ran (e.g. fast-fail before the marker), the
    cancel helper appends a fresh assistant message instead of failing."""
    task = _make_task(db, name="Cancel Chat Test 2")
    ex = _make_execution(db, task, status="running")
    try:
        # Make sure ensure-session has run so the helper has a session
        # to write to, but do NOT call _post_run_request_marker.
        from app.services.automation_sessions import ensure_task_chat_session
        ensure_task_chat_session(db, task)
        db.refresh(task)
        from app.services.automation_executor import _persist_cancellation_to_chat
        _persist_cancellation_to_chat(db, task, ex)

        msgs = [
            m for m in db.query(ChatMessage).filter(
                ChatMessage.session_id == task.session_id,
            ).all() if m.role == "assistant"
        ]
        assert len(msgs) == 1, "should append exactly one assistant row"
        assert "Run cancelled by user" in msgs[0].content
    finally:
        _teardown(db, task, ex)


def test_persist_cancellation_never_raises():
    """A chat-write failure must not propagate: the cancel endpoint
    must remain fast and the DB flip is the source of truth."""
    from app.services.automation_executor import _persist_cancellation_to_chat

    class _BrokenTask:
        id = "no-such-task"
        session_id = None
        name = "broken"
        type = "report"
        project = "p"
        description = "d"
        output_format = "docx"

    class _BrokenExe:
        id = "no-such-exe"

    # Should not raise even with garbage inputs.
    _persist_cancellation_to_chat(None, _BrokenTask(), _BrokenExe())
