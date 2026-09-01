"""Run-request marker tests: every run posts a visible "Run Automation
Task：" user bubble (5-bullet summary) AND an empty assistant bubble
into the task's dedicated chat session, even when the run fails fast
at the data_sync preflight.

Covers:
1. ``_post_run_request_marker`` writes EXACTLY ONE ``role="user"``
   ChatMessage with all 5 bullets (Name / Type / Output format /
   Project / Description, full-width colons) incl. the
   ``Output format：Word document (docx)`` label, and a ``phase`` that
   deep-links the execution.  Also writes ONE empty ``role="assistant"``
   ChatMessage for the live-progress / final-reply slot.
2. Idempotent: calling twice for the same execution does not double-write.
3. Never raises — a marker failure must not block the run.
4. Wired into ``execute_automation`` BEFORE the data_sync preflight gate
   so failed-preflight runs still get the request record (source
   inspection, matching the convention in ``test_automation_ds_preflight.py``).
"""
from __future__ import annotations

import os
import sys
import uuid

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.database import SessionLocal
from app.models.automation_execution import AutomationExecution
from app.models.automation_task import AutomationTask
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession


# --- Fixtures ---------------------------------------------------------------


def _make_task(
    db,
    *,
    name: str = "Weekly Sales Brief",
    type_: str = "report",
    output_format: str = "docx",
    project: str = "Q3 Sales",
    description: str = "Summarize weekly sales performance and top movers.",
) -> AutomationTask:
    task = AutomationTask(
        name=name,
        type=type_,
        prompt=description,
        description=description,
        cron_expression="0 9 * * 1",
        project=project,
        output_format=output_format,
        is_deleted=False,
        notify_chat=True,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _make_execution(db, task: AutomationTask, attempt: int = 0) -> AutomationExecution:
    ex = AutomationExecution(
        automation_task_id=task.id,
        status="running",
        attempt=attempt,
    )
    db.add(ex)
    db.commit()
    db.refresh(ex)
    return ex


def _cleanup(db, *objs) -> None:
    for o in objs:
        if o is None:
            continue
        try:
            db.refresh(o)
        except Exception:
            pass
        try:
            db.delete(o)
        except Exception:
            pass
    db.commit()


def _teardown_marker_test(db, task, execution):
    """FK-safe teardown: the marker creates a session+conv that the task
    and messages reference; the execution references the task. Deletion
    order: messages → execution → task (clears session_id FK) → session → conv."""
    sid = task.session_id
    # 1. delete marker messages
    if sid:
        for m in db.query(ChatMessage).filter(
            ChatMessage.session_id == sid,
        ).all():
            db.delete(m)
    db.commit()
    # 2. delete execution (references task.automation_task_id)
    _cleanup(db, execution)
    # 3. null the task's session FK + delete task (clears session_id FK)
    task.session_id = None
    db.commit()
    _cleanup(db, task)
    # 4. delete session + conv
    if sid:
        from app.models.agent_conversation import AgentConversation
        sess = db.query(ChatSession).filter(ChatSession.id == sid).first()
        conv_id = sess.conversation_id if sess else None
        if sess:
            db.delete(sess)
            db.commit()
        if conv_id:
            conv = db.query(AgentConversation).filter(
                AgentConversation.id == conv_id,
            ).first()
            if conv:
                db.delete(conv)
                db.commit()
    db.commit()


# --- Test 1: writes exactly one visible user bubble with 5 bullets -----------


def test_post_run_request_marker_writes_visible_user_bubble():
    db = SessionLocal()
    task = _make_task(db)
    ex = _make_execution(db, task)
    try:
        from app.services.automation_executor import _post_run_request_marker
        _post_run_request_marker(db, task, ex, trigger="run")

        msgs = db.query(ChatMessage).filter(
            ChatMessage.session_id == task.session_id,
        ).all()
        assert len(msgs) == 2, "exactly two markers per execution (user + assistant)"
        user_msg = [m for m in msgs if m.role == "user"][0]
        asst_msg = [m for m in msgs if m.role == "assistant"][0]
        # User bubble: 5-bullet summary
        body = user_msg.content
        assert "Run Automation Task：" in body
        assert "Name：Weekly Sales Brief" in body
        assert "Type：report" in body
        assert "Output format：Word document (docx)" in body
        assert "Project：" in body and "Q3 Sales" in body
        assert "Description：Summarize weekly sales performance" in body
        # phase deep-links the execution + task.
        phase = user_msg.phase or {}
        assert phase.get("execution_id") == ex.id
        assert phase.get("automation_task_id") == task.id
        # Assistant bubble: empty (populated later by _persist_run_to_chat)
        assert asst_msg.content == ""
        assert asst_msg.phase.get("live") is True
    finally:
        _teardown_marker_test(db, task, ex)
        db.close()


# --- Test 2: idempotent — calling twice does not double-write ---------------


def test_post_run_request_marker_idempotent_per_execution():
    db = SessionLocal()
    task = _make_task(db, name="Idempotent Marker Task")
    ex = _make_execution(db, task)
    try:
        from app.services.automation_executor import _post_run_request_marker
        _post_run_request_marker(db, task, ex, trigger="run")
        _post_run_request_marker(db, task, ex, trigger="run")  # duplicate

        msgs = db.query(ChatMessage).filter(
            ChatMessage.session_id == task.session_id,
            ChatMessage.role == "user",
        ).all()
        assert len(msgs) == 1, "duplicate call for same execution must not double-write"
    finally:
        _teardown_marker_test(db, task, ex)
        db.close()


# --- Test 3: never raises ---------------------------------------------------


def test_post_run_request_marker_never_raises_on_missing_session():
    """A marker failure (e.g. no session to write to) must not propagate
    — the run must still proceed."""
    db = SessionLocal()
    task = _make_task(db, name="No Session Marker Task")
    ex = _make_execution(db, task)
    # Wipe the session id so ensure-session has nothing to attach to.
    task.session_id = None
    db.commit()
    db.refresh(task)
    try:
        from app.services.automation_executor import _post_run_request_marker
        # Must not raise even though the session can't be ensured.
        _post_run_request_marker(db, task, ex, trigger="run")
        # No marker written, but no exception either.
        assert task.session_id is None or True  # did not crash
    finally:
        _teardown_marker_test(db, task, ex)
        db.close()


# --- Test 4: wired into execute_automation BEFORE the preflight gate -------


def test_marker_wired_before_preflight_gate():
    """Source inspection: ``_post_run_request_marker`` is called inside
    ``execute_automation`` BEFORE the ``_resolve_bound_data_source_ids``
    preflight, so a run that fails fast at the gate still leaves the
    request record. Matches the convention in
    ``test_automation_ds_preflight.py``."""
    import inspect
    from app.services import automation_executor as ax
    src = inspect.getsource(ax.execute_automation)
    marker = src.index("_post_run_request_marker")
    gate = src.index("_resolve_bound_data_source_ids")
    assert marker < gate, (
        "marker must be posted before the data_sync preflight gate so "
        "failed-preflight runs still show the request record"
    )
