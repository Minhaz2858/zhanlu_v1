"""Tests for the AUTOMATION FAILURE-PATH persist contract.

The chat frontend loads from `chat_messages` (not the v3 stream's
`agent_conversations.messages` JSON), so for the user to see *anything*
happen after the auto-prompt is injected, the executor MUST drop a
visible assistant bubble into the task's dedicated chat session on
EVERY exit path — not just on success.

The executor wraps its agent.run() block with try/finally and a
`_persist_state` dict that captures the assistant text on every
exception path (FuturesTimeout, _AutomationPaused,
_TaskCreatorMissingError, generic Exception, tool-failure gate).
This test verifies the persist function correctly emits the
assistant bubble for each of those cases.

Covers:
1. Timeout: assistant bubble carries a ⏱ message naming the timeout.
2. Paused: assistant bubble carries a ⏸ message + resume hint.
3. Missing creator: assistant bubble carries a 👤 message.
4. Generic exception: assistant bubble carries a ❌ message with the
   underlying error text.
5. Tool-failure gate: assistant bubble carries a ⚠ message + the first
   tool errors.
6. Each of these respects role-aware idempotency: if the auto-prompt
   marker is already in chat_messages, the persist does NOT re-write
   the user bubble (no duplicate).
"""
from __future__ import annotations

import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.automation_execution import AutomationExecution
from app.models.automation_task import AutomationTask
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.services import automation_executor as ax


# --- Fixtures --------------------------------------------------------------


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


def _make_task(
    db,
    *,
    name: str = "Failure-Path Test Task",
    description: str = "Test task that simulates agent failure paths.",
) -> AutomationTask:
    # Create a dedicated automation chat session first so task.session_id
    # is valid AND ensure_task_chat_session reuses it (it reuses any
    # existing session, including dedicated ones).
    import uuid as _uuid
    sess = ChatSession(
        id=str(_uuid.uuid4()),
        title=name,
        agent_name="automation_agent",
    )
    db.add(sess)
    db.flush()
    task = AutomationTask(
        name=name,
        type="data_sync",
        prompt=description,
        description=description,
        cron_expression="0 8 * * *",
        project="Ecisco BI",
        output_format="html",
        is_deleted=False,
        notify_chat=True,
        session_id=sess.id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _make_execution(db, task: AutomationTask) -> AutomationExecution:
    ex = AutomationExecution(
        automation_task_id=task.id,
        status="failed",
        attempt=0,
    )
    db.add(ex)
    db.commit()
    db.refresh(ex)
    return ex


def _seed_auto_prompt_marker(db, task, ex):
    """Simulate the auto-prompt user bubble written by
    _post_run_request_marker at the start of execute_automation."""
    import uuid as _uuid
    m = ChatMessage(
        id=str(_uuid.uuid4()),
        session_id=task.session_id,
        role="user",
        content=f"Run Automation Task:  Name: {task.name}  Type: data_sync  Project: Ecisco BI",
        order=0,
        phase={
            "verb": "▶",
            "title": "Run Automation Task",
            "execution_id": ex.id,
            "automation_task_id": task.id,
            "trigger": "run",
        },
    )
    db.add(m)
    db.commit()
    return m


# --- Test 1: Timeout (the actual user-reported failure) -------------------


def test_failure_path_timeout_persists_assistant_bubble(db):
    task = _make_task(db, name="Timeout Task")
    ex = _make_execution(db, task)
    _seed_auto_prompt_marker(db, task, ex)

    # Simulate the finally-block behavior: timeout path sets
    # _persist_state["assistant_text"] and the finally block calls
    # _persist_run_to_chat.
    timeout_s = 600
    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt=None,  # marker already exists
        assistant_text=(
            f"⏱ The run exceeded the {timeout_s}s time limit "
            f"and was stopped. The agent may have hit a slow tool "
            f"call or a network issue. Try again, or simplify the "
            f"task description to lower the cost."
        ),
    )

    msgs = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
    ).order_by(ChatMessage.order.asc()).all()

    roles = [m.role for m in msgs]
    assert roles == ["user", "assistant"], f"expected user+assistant pair, got {roles}"

    asst = next(m for m in msgs if m.role == "assistant")
    assert "⏱" in asst.content
    assert str(timeout_s) in asst.content
    ph = asst.phase or {}
    assert ph.get("execution_id") == ex.id
    assert ph.get("automation_task_id") == task.id


# --- Test 2: Paused --------------------------------------------------------


def test_failure_path_paused_persists_assistant_bubble(db):
    task = _make_task(db, name="Paused Task")
    ex = _make_execution(db, task)
    _seed_auto_prompt_marker(db, task, ex)

    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt=None,
        assistant_text=(
            "⏸ The run was paused for approval: "
            "awaiting operator review. "
            "Resume it from the task detail page to continue."
        ),
    )

    msgs = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
    ).order_by(ChatMessage.order.asc()).all()
    asst = next(m for m in msgs if m.role == "assistant")
    assert "⏸" in asst.content
    assert "Resume" in asst.content


# --- Test 3: Missing creator ----------------------------------------------


def test_failure_path_missing_creator_persists_assistant_bubble(db):
    task = _make_task(db, name="Missing Creator Task")
    ex = _make_execution(db, task)
    _seed_auto_prompt_marker(db, task, ex)

    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt=None,
        assistant_text=(
            "👤 The task creator's account is no longer in the "
            "system. Re-save the task to assign a new creator, "
            "then run again."
        ),
    )

    msgs = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
    ).order_by(ChatMessage.order.asc()).all()
    asst = next(m for m in msgs if m.role == "assistant")
    assert "👤" in asst.content
    assert "Re-save" in asst.content


# --- Test 4: Generic exception --------------------------------------------


def test_failure_path_generic_exception_persists_assistant_bubble(db):
    task = _make_task(db, name="Generic Error Task")
    ex = _make_execution(db, task)
    _seed_auto_prompt_marker(db, task, ex)

    err = ValueError("tool handler exploded")
    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt=None,
        assistant_text=(
            f"❌ The agent run failed: {err}\n\n"
            f"Check the execution logs for full detail. "
            f"Please fix the issue and try again."
        ),
    )

    msgs = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
    ).order_by(ChatMessage.order.asc()).all()
    asst = next(m for m in msgs if m.role == "assistant")
    assert "❌" in asst.content
    assert "tool handler exploded" in asst.content


# --- Test 5: Tool-failure gate --------------------------------------------


def test_failure_path_tool_failure_gate_persists_assistant_bubble(db):
    task = _make_task(db, name="Tool Failure Task")
    ex = _make_execution(db, task)
    _seed_auto_prompt_marker(db, task, ex)

    errors = [
        "execute_query: connection refused (10.10.10.49:3306)",
        "describe_schema: timeout after 30s",
    ]
    _errs = "; ".join(errors[:3])
    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt=None,
        assistant_text=(
            f"⚠ The run could not complete its work — all "
            f"2 tool call(s) failed.\n\n"
            f"First errors: {_errs}\n\n"
            f"The data source may be unreachable, or the task "
            f"description may need adjusting. A retry has been "
            f"scheduled."
        ),
    )

    msgs = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
    ).order_by(ChatMessage.order.asc()).all()
    asst = next(m for m in msgs if m.role == "assistant")
    assert "⚠" in asst.content
    assert "connection refused" in asst.content
    assert "retry has been scheduled" in asst.content


# --- Test 6: Idempotency — failure persist does NOT duplicate user bubble -


def test_failure_path_does_not_duplicate_user_marker(db):
    """The auto-prompt marker was already written by _post_run_request_marker
    at the start of execute_automation. The finally-block persist must NOT
    re-write the user bubble (which would create a duplicate user bubble
    showing two "Run Automation Task" prompts)."""
    task = _make_task(db, name="No Duplicate Task")
    ex = _make_execution(db, task)
    _seed_auto_prompt_marker(db, task, ex)

    # Simulate the finally block: pass user_prompt=None, only write assistant
    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt=None,
        assistant_text="⏱ Run timed out after 600s.",
    )

    msgs = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
    ).order_by(ChatMessage.order.asc()).all()
    user_msgs = [m for m in msgs if m.role == "user"]
    asst_msgs = [m for m in msgs if m.role == "assistant"]

    assert len(user_msgs) == 1, (
        f"expected exactly ONE user bubble (the marker), got {len(user_msgs)}"
    )
    assert len(asst_msgs) == 1, (
        f"expected exactly ONE assistant bubble (the failure msg), got {len(asst_msgs)}"
    )
