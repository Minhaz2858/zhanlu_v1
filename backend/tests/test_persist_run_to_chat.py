"""Tests for ``_persist_run_to_chat``: every successful run writes the
user prompt and the assistant's final reply into the task's dedicated
chat session, so the chat frontend can render the run timeline.

Covers:
1. ``_persist_run_to_chat`` writes BOTH a user bubble (5-bullet summary
   as a fallback when the marker hasn't run) and an assistant bubble
   (carrying the final reply) into the task's dedicated chat session.
2. Idempotent: calling twice for the same execution does not double-write.
3. Empty assistant_text is allowed (an aborted run still records the
   user-side prompt, and the assistant row is skipped, never raised).
4. The chat_messages rows carry the correct session_id, role, and phase
   so the frontend can deep-link the execution.
5. Never raises — a persist failure must not block the run.
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
from app.models.automation_file import AutomationFile
from app.models.automation_task import AutomationTask
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.services import automation_executor as ax


# --- Fixtures --------------------------------------------------------------


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


def _make_task(
    db,
    *,
    name: str = "Daily Sales Data Sync",
    type_: str = "data_sync",
    output_format: str = "html",
    project: str = "Ecisco BI",
    description: str = "Sync ERP sales data into the business database.",
) -> AutomationTask:
    task = AutomationTask(
        name=name,
        type=type_,
        prompt=description,
        description=description,
        cron_expression="0 8 * * *",
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


# --- Test 1: writes both user + assistant bubbles -------------------------


def test_persist_run_to_chat_writes_user_and_assistant_bubbles(db):
    task = _make_task(db)
    ex = _make_execution(db, task)
    user_prompt = "Run the daily sales data sync. Sync ERP sales data into the business database."
    assistant_text = "Sync complete. 124 new orders, 7 anomalies flagged, HTML report written."

    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt=user_prompt,
        assistant_text=assistant_text,
    )

    msgs = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
    ).order_by(ChatMessage.order.asc()).all()
    assert len(msgs) == 2, f"expected user+assistant pair, got {len(msgs)}"

    user_m = next(m for m in msgs if m.role == "user")
    asst_m = next(m for m in msgs if m.role == "assistant")
    # The user bubble carries the 5-bullet summary (fallback from _persist_run_to_chat).
    assert "Run Automation Task" in user_m.content
    assert "Name：" in user_m.content
    # The assistant bubble carries the final reply.
    assert asst_m.content == assistant_text, "assistant bubble carries the final reply"

    # Phase deep-links the execution + task for the UI.
    for m in (user_m, asst_m):
        ph = m.phase or {}
        assert ph.get("execution_id") == ex.id
        assert ph.get("automation_task_id") == task.id


# --- Test 2: idempotent per execution --------------------------------------


def test_persist_run_to_chat_idempotent_per_execution(db):
    task = _make_task(db, name="Idempotent Persist Task")
    ex = _make_execution(db, task)
    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt="p1",
        assistant_text="r1",
    )
    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt="p1 (retry)",
        assistant_text="r1 (retry)",
    )
    msgs = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
    ).all()
    assert len(msgs) == 2, "duplicate call for same execution must not double-write"


# --- Test 3: empty assistant text is allowed (no crash, no assistant row) --


def test_persist_run_to_chat_allows_empty_assistant_text(db):
    task = _make_task(db, name="Empty Assistant Task")
    ex = _make_execution(db, task)
    # Aborted run: prompt was sent, but agent produced no final text.
    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt="Sync please",
        assistant_text="",
    )
    msgs = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
    ).all()
    roles = sorted(m.role for m in msgs)
    assert roles == ["user"], f"only the user bubble should be written, got {roles}"
    # The user bubble should be the 5-bullet summary format.
    assert "Run Automation Task" in msgs[0].content


# --- Test 4: never raises on missing session -------------------------------


def test_persist_run_to_chat_never_raises_on_missing_session(db):
    task = _make_task(db, name="No Session Persist Task")
    ex = _make_execution(db, task)
    # Force the dedicated session to be missing so ensure_task_chat_session
    # must run; the function must not raise.
    task.session_id = None
    db.commit()
    db.refresh(task)
    # Must not raise even though no session can be ensured.
    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt="x",
        assistant_text="y",
    )


# --- Test 5: assistant bubble carries file artifacts ------------------------
# The assistant bubble must carry the run's AutomationFile outputs as
# artifacts (source == "automation_file") so the chat renders inline preview
# cards. Deleted files are excluded; no files → artifacts stays None.


def _make_file(
    db,
    execution: AutomationExecution,
    *,
    name: str,
    file_type: str,
    is_deleted: bool = False,
) -> AutomationFile:
    f = AutomationFile(
        execution_id=execution.id,
        automation_task_id=execution.automation_task_id,
        name=name,
        file_type=file_type,
        size=1024,
        is_deleted=is_deleted,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def test_persist_run_to_chat_attaches_automation_file_artifacts(db):
    task = _make_task(db)
    ex = _make_execution(db, task)
    f1 = _make_file(db, ex, name="report.html", file_type="html")
    f2 = _make_file(db, ex, name="data.csv", file_type="csv")
    # A soft-deleted file must NOT be attached.
    _make_file(db, ex, name="deleted.docx", file_type="docx", is_deleted=True)

    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt="run",
        assistant_text="done",
    )

    asst_m = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
        ChatMessage.role == "assistant",
    ).one()
    assert asst_m.artifacts is not None, "assistant bubble must carry artifacts"
    assert len(asst_m.artifacts) == 2, "soft-deleted files must be excluded"
    assert {a["id"] for a in asst_m.artifacts} == {f1.id, f2.id}
    for a in asst_m.artifacts:
        assert a["source"] == "automation_file"
        assert a.get("has_preview") is True


def test_persist_run_to_chat_no_files_keeps_artifacts_none(db):
    task = _make_task(db)
    ex = _make_execution(db, task)

    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt="run",
        assistant_text="done",
    )

    asst_m = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
        ChatMessage.role == "assistant",
    ).one()
    assert asst_m.artifacts is None


def test_persist_run_to_chat_updates_existing_bubble_artifacts(db):
    from app.services.automation_sessions import ensure_task_chat_session
    task = _make_task(db)
    ex = _make_execution(db, task)
    f1 = _make_file(db, ex, name="report.html", file_type="html")

    session_id, _ = ensure_task_chat_session(db, task)

    # Simulate the empty assistant bubble pre-created by
    # _post_run_request_marker (role=assistant, content="", phase carries the
    # execution id). _persist_run_to_chat must update THIS bubble in place.
    db.add(ChatMessage(
        session_id=session_id,
        role="assistant",
        content="",
        order=1,
        artifacts=None,
        phase={"execution_id": ex.id, "automation_task_id": task.id},
    ))
    db.commit()

    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt="run",
        assistant_text="done",
    )

    asst_m = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
        ChatMessage.role == "assistant",
    ).one()
    assert asst_m.content == "done"
    assert asst_m.artifacts is not None, "in-place update must also attach artifacts"
    assert len(asst_m.artifacts) == 1
    assert asst_m.artifacts[0]["source"] == "automation_file"


# --- Test 6: production ordering (persist before files, then re-persist) ---
# The real execute_automation flow calls _persist_run_to_chat in the inner
# `finally` BEFORE _render_and_save_files creates the AutomationFile rows,
# so the first call must write artifacts=None. A second call AFTER file
# generation must attach the now-existing files onto the SAME bubble. This
# is the regression guard for the "still no file showing" bug.


def test_persist_run_to_chat_second_call_attaches_late_files(db):
    task = _make_task(db)
    ex = _make_execution(db, task)

    # Mirror production: persist when NO files exist yet (the inner finally).
    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt="run",
        assistant_text="done",
    )

    asst_m = db.query(ChatMessage).filter(
        ChatMessage.session_id == task.session_id,
        ChatMessage.role == "assistant",
    ).one()
    assert asst_m.artifacts is None, "first persist (pre-render) sees zero files"

    # Now files are created (mirrors _render_and_save_files ordering).
    f1 = _make_file(db, ex, name="report.html", file_type="html")
    f2 = _make_file(db, ex, name="data.csv", file_type="csv")

    # Second persist attaches the freshly-committed artifacts in place.
    ax._persist_run_to_chat(
        db, task, ex,
        user_prompt="run",
        assistant_text="done",
    )

    db.refresh(asst_m)
    assert asst_m.artifacts is not None, "second persist must attach late files"
    assert {a["id"] for a in asst_m.artifacts} == {f1.id, f2.id}
    for a in asst_m.artifacts:
        assert a["source"] == "automation_file"
