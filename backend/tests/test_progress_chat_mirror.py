"""Test for the live progress mirror from execution -> chat timeline.

When the agent runs, ``_persist_run_progress`` records
``activity_steps`` on the execution row. The chat frontend loads from
``chat_messages`` and renders ``activity_steps`` as numbered tool
cards (the same 1/2/3/.../7 list the general_assistant stream shows).

Without mirroring, the user only sees the 3-placeholders frontend
skeleton because the automation executor runs in a background thread
and no SSE events flow to the chat. With the mirror, the run's
ASSISTANT bubble (pre-created empty by _post_run_request_marker) gets
its ``activity_steps`` updated as tools fire, so the user sees the
real steps in their chat timeline. The frontend only renders
ActivitySteps on assistant bubbles, so mirroring to the user bubble
would have no visible effect.
"""
from __future__ import annotations

import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import uuid as _uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.automation_execution import AutomationExecution
from app.models.automation_task import AutomationTask
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.services import automation_executor as ax


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


@pytest.fixture
def patched_session_local(db):
    """Patch SessionLocal in app.database so _persist_run_progress opens
    a fresh session bound to the same in-memory engine (the original
    fixture session must not be closed by the executor's finally block)."""
    from app.database import SessionLocal as _RealSessionLocal
    engine = db.get_bind()
    Session = sessionmaker(bind=engine)

    import app.database as _appdb_mod
    _saved = _appdb_mod.SessionLocal
    _appdb_mod.SessionLocal = Session
    try:
        yield Session
    finally:
        _appdb_mod.SessionLocal = _saved


def _make_task_and_marker(db):
    """Create a task, execution, and both user + assistant bubbles.

    The assistant bubble is pre-created empty (as _post_run_request_marker
    does in production) so _persist_run_progress can mirror activity_steps
    onto it. The frontend only renders ActivitySteps on assistant bubbles.
    """
    sess = ChatSession(
        id=str(_uuid.uuid4()),
        title="Mirror Test",
        agent_name="automation_agent",
    )
    db.add(sess)
    db.flush()
    task = AutomationTask(
        name="Mirror Test",
        type="data_sync",
        prompt="Sync daily sales.",
        description="Sync daily sales.",
        cron_expression="0 8 * * *",
        project="Ecisco BI",
        output_format="html",
        is_deleted=False,
        notify_chat=True,
        session_id=sess.id,
    )
    db.add(task)
    db.flush()
    ex = AutomationExecution(
        automation_task_id=task.id,
        status="running",
        attempt=0,
    )
    db.add(ex)
    db.flush()
    # The auto-prompt user bubble (matched by phase.execution_id)
    user_msg = ChatMessage(
        id=str(_uuid.uuid4()),
        session_id=sess.id,
        role="user",
        content="Run Automation Task: ...",
        order=0,
        activity_steps=None,
        phase={
            "verb": "▶",
            "title": "Run Automation Task",
            "execution_id": ex.id,
            "automation_task_id": task.id,
        },
    )
    db.add(user_msg)
    # The pre-created empty assistant bubble — this is where live
    # progress gets mirrored. The frontend only renders ActivitySteps
    # on assistant bubbles, so this is the target for the mirror.
    asst_msg = ChatMessage(
        id=str(_uuid.uuid4()),
        session_id=sess.id,
        role="assistant",
        content="",
        order=1,
        activity_steps=[],
        phase={
            "verb": "🤖",
            "title": "Run Automation Task",
            "execution_id": ex.id,
            "automation_task_id": task.id,
            "live": True,
        },
    )
    db.add(asst_msg)
    db.commit()
    db.refresh(user_msg)
    db.refresh(asst_msg)
    return task, ex, user_msg, asst_msg


def test_persist_run_progress_mirrors_steps_to_assistant_bubble(db, patched_session_local):
    """A call to _persist_run_progress(execution_id, steps, ...) must
    update the assistant bubble's activity_steps so the chat timeline shows
    live per-step tool progress (like the general_assistant stream).

    The frontend only renders ActivitySteps on assistant bubbles, so
    mirroring to the user bubble would have no visible effect.
    """
    task, ex, user_msg, asst_msg = _make_task_and_marker(db)

    steps = [
        {"number": 1, "description": "Understanding your request", "status": "done", "duration_ms": 4},
        {"number": 2, "description": "Querying the bound data source", "status": "running", "tool_name": "ask_data_agent"},
    ]
    ax._persist_run_progress(execution_id=ex.id, steps=steps, phase="act")

    db.expire_all()
    db.refresh(asst_msg)

    assert asst_msg.activity_steps is not None, (
        "assistant bubble's activity_steps should be set after _persist_run_progress"
    )
    assert len(asst_msg.activity_steps) == 2
    assert asst_msg.activity_steps[0]["description"] == "Understanding your request"
    assert asst_msg.activity_steps[1]["tool_name"] == "ask_data_agent"


def test_persist_run_progress_updates_execution_row_first(db, patched_session_local):
    """The execution row should still get the activity_steps update too
    (the Scheduled panel reads from automation_executions)."""
    task, ex, _, _ = _make_task_and_marker(db)

    steps = [
        {"number": 1, "description": "Step one", "status": "done"},
        {"number": 2, "description": "Step two", "status": "running"},
    ]
    ax._persist_run_progress(execution_id=ex.id, steps=steps, phase="act")

    db.expire_all()
    db.refresh(ex)
    assert ex.activity_steps is not None
    assert len(ex.activity_steps) == 2


def test_persist_run_progress_does_not_touch_other_executions_bubbles(db, patched_session_local):
    """Mirror must scope by execution_id, not touch unrelated assistant bubbles."""
    task, ex, user_msg, asst_msg = _make_task_and_marker(db)
    # A second execution's assistant bubble, in the same session
    other_ex = AutomationExecution(
        automation_task_id=task.id,
        status="running",
        attempt=0,
    )
    db.add(other_ex)
    db.flush()
    other_asst_msg = ChatMessage(
        id=str(_uuid.uuid4()),
        session_id=user_msg.session_id,
        role="assistant",
        content="",
        order=2,
        activity_steps=[],
        phase={"execution_id": other_ex.id, "live": True},
    )
    db.add(other_asst_msg)
    db.commit()

    steps = [{"number": 1, "description": "My step", "status": "done"}]
    ax._persist_run_progress(execution_id=ex.id, steps=steps, phase="act")

    db.expire_all()
    db.refresh(asst_msg)
    db.refresh(other_asst_msg)

    assert asst_msg.activity_steps is not None, "matched assistant bubble must update"
    assert len(asst_msg.activity_steps) == 1
    assert other_asst_msg.activity_steps == [], "unmatched assistant bubble must NOT update"


def test_persist_run_progress_partial_text_writes_to_execution(db, patched_session_local):
    """When partial_text is supplied, the execution's output_text gets it
    so a hung-LLM timeout retains whatever the agent produced."""
    task, ex, _, _ = _make_task_and_marker(db)

    steps = [{"number": 1, "description": "Step one", "status": "done"}]
    ax._persist_run_progress(
        execution_id=ex.id,
        steps=steps,
        phase="act",
        partial_text="partial agent text so far",
    )

    db.expire_all()
    db.refresh(ex)
    assert ex.output_text == "partial agent text so far"


def test_persist_run_progress_no_partial_text_leaves_output_alone(db, patched_session_local):
    """A progress-only write (partial_text=None) must NOT clobber the
    execution's existing output_text."""
    task, ex, _, _ = _make_task_and_marker(db)
    ex.output_text = "previously written output"
    db.commit()

    steps = [{"number": 1, "description": "Step one", "status": "done"}]
    ax._persist_run_progress(execution_id=ex.id, steps=steps, phase="act")

    db.expire_all()
    db.refresh(ex)
    assert ex.output_text == "previously written output"
