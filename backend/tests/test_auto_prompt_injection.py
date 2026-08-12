"""Tests for the auto-prompt injection at run start.

Per spec: every automation run must immediately create BOTH a user bubble
(synthetic "Run Automation Task: …" card) AND an empty assistant bubble
in the task's dedicated chat session BEFORE the agent starts processing.

Covers:
1. ``_post_run_request_marker`` is called at the start of the run (it
   persists a user bubble with the 5-bullet summary AND an EMPTY
   assistant bubble for the activity_steps mirror).
2. The marker is visible to the user IMMEDIATELY (no waiting for the
   agent to finish).
3. ``_persist_run_to_chat`` (called later) writes the final content
   into the pre-created assistant bubble.
4. The marker carries ``phase.execution_id`` so the frontend can deep-link.
"""
from __future__ import annotations

import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.agent_conversation import AgentConversation
from app.models.automation_execution import AutomationExecution
from app.models.automation_task import AutomationTask
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.services import automation_executor as ax


@pytest.fixture
def db():
    """Fresh in-memory SQLite with all tables created via the full model
    registry — same code path the real backend uses."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _make_task(db, name="Daily Sales Data Sync", type_="data_sync"):
    task = AutomationTask(
        name=name,
        type=type_,
        prompt="Sync ERP sales data to the business database daily.",
        description="Sync ERP sales data to the business database daily with incremental updates and anomaly alerts.",
        cron_expression="0 8 * * *",
        project="Ecisco BI",
        output_format="html",
        is_deleted=False,
        notify_chat=True,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _make_execution(db, task, attempt=0):
    ex = AutomationExecution(
        automation_task_id=task.id,
        status="queued",
        attempt=attempt,
    )
    db.add(ex)
    db.commit()
    db.refresh(ex)
    return ex


# --- Test 1: marker creates user bubble + empty assistant bubble ---


def test_post_run_request_marker_creates_user_and_assistant_bubble(db):
    """``_post_run_request_marker`` writes a user bubble (5-bullet summary)
    and an EMPTY assistant bubble (for the activity_steps mirror) into the
    task's dedicated session."""
    task = _make_task(db)
    ex = _make_execution(db, task)
    ax._post_run_request_marker(db, task, ex, trigger="run")

    msgs = db.query(ChatMessage).filter(ChatMessage.session_id == task.session_id).all()
    assert len(msgs) == 2, f"expected 2 bubbles (user + assistant), got {len(msgs)}"
    user_msg = [m for m in msgs if m.role == "user"][0]
    asst_msg = [m for m in msgs if m.role == "assistant"][0]
    # User bubble: 5-bullet summary
    assert "Run Automation Task" in user_msg.content
    assert "Name：" in user_msg.content
    # Assistant bubble: empty (pre-created for activity_steps mirror)
    assert asst_msg.content == "", "assistant bubble should be empty (pre-created)"
    assert asst_msg.activity_steps == [], "assistant bubble should have empty activity_steps"
    # Phase deep-links the execution.
    for m in (user_msg, asst_msg):
        assert m.phase["execution_id"] == ex.id
        assert m.phase["automation_task_id"] == task.id
    assert asst_msg.phase["trigger"] == "run"
    assert asst_msg.phase["live"] is True


# --- Test 2: marker is idempotent per execution ----------------------------


def test_post_run_request_marker_idempotent_per_execution(db):
    """Calling the marker twice for the same execution is a no-op."""
    task = _make_task(db)
    ex = _make_execution(db, task)
    ax._post_run_request_marker(db, task, ex, trigger="run")
    ax._post_run_request_marker(db, task, ex, trigger="run")

    msgs = db.query(ChatMessage).filter(ChatMessage.session_id == task.session_id).all()
    assert len(msgs) == 2, f"second call should be a no-op, got {len(msgs)}"


# --- Test 3: persist_run_to_chat writes into the pre-created assistant bubble ---


def test_persist_run_to_chat_writes_into_precreated_assistant_bubble(db):
    """If the marker is already there, ``_persist_run_to_chat`` must
    write the assistant content into the pre-created bubble. The user
    bubble from the marker is kept (not duplicated)."""
    task = _make_task(db)
    ex = _make_execution(db, task)
    # Step 1: marker is posted at the start of the run.
    ax._post_run_request_marker(db, task, ex, trigger="run")
    # Step 2: agent finishes; _persist_run_to_chat is called with the
    # user_prompt + assistant_text.
    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt="Sync ERP sales data to the business database daily.",
        assistant_text="Sync complete. 124 new orders, 7 anomalies flagged.",
    )

    msgs = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
    ).order_by(ChatMessage.order).all()
    assert len(msgs) == 2, f"expected user+assistant pair, got {len(msgs)}"
    user_m = next(m for m in msgs if m.role == "user")
    asst_m = next(m for m in msgs if m.role == "assistant")
    # The user bubble carries the 5-bullet summary from the marker.
    assert "Run Automation Task" in user_m.content
    # The assistant bubble carries the final reply.
    assert "Sync complete" in asst_m.content


# --- Test 4: marker is visible immediately (before agent finishes) --------


def test_marker_visible_before_agent_runs(db):
    """The marker is persisted synchronously — visible to the user the
    moment ``_post_run_request_marker`` returns, no waiting for the
    agent. This is the core spec requirement."""
    task = _make_task(db)
    ex = _make_execution(db, task)
    ax._post_run_request_marker(db, task, ex, trigger="run")

    # No agent run, no _persist_run_to_chat, no agent response.
    # The user bubble + empty assistant bubble are still there.
    msgs = db.query(ChatMessage).filter(ChatMessage.session_id == task.session_id).all()
    assert len(msgs) == 2
    user_msg = [m for m in msgs if m.role == "user"][0]
    asst_msg = [m for m in msgs if m.role == "assistant"][0]
    assert "Run Automation Task" in user_msg.content
    assert asst_msg.content == ""
