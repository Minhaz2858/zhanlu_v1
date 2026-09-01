"""Unit tests for the one-task-one-session backfill / ensure-session flow."""
from __future__ import annotations

from app.database import SessionLocal
from app.models.automation_task import AutomationTask
from app.models.chat_session import ChatSession


def _make_task(db, name: str = "Daily Sales Data Sync") -> AutomationTask:
    """Create a minimal AutomationTask row for testing."""
    task = AutomationTask(
        name=name,
        type="scheduled",
        prompt="Test prompt",
        cron_expression="0 9 * * *",
        project="global",
        is_deleted=False,
        notify_chat=True,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _cleanup_task(db, task: AutomationTask) -> None:
    """Remove a task + its associated ChatSession + AgentConversation."""
    from app.models.agent_conversation import AgentConversation
    if task.session_id:
        chat = db.query(ChatSession).filter(ChatSession.id == task.session_id).first()
        if chat is not None:
            conv_id = chat.conversation_id
            db.delete(chat)
            if conv_id:
                conv = db.query(AgentConversation).filter(AgentConversation.id == conv_id).first()
                if conv is not None:
                    db.delete(conv)
    db.delete(task)
    db.commit()


def test_ensure_session_creates_new_when_none():
    """A task with session_id=None gets a dedicated chat session."""
    db = SessionLocal()
    task = _make_task(db, name="Sync Test A")
    try:
        assert task.session_id is None
        from app.routers.automation_api import _ensure_task_chat_session
        session_id, created = _ensure_task_chat_session(db, task)
        assert created is True
        assert session_id
        db.refresh(task)
        assert task.session_id == session_id
        chat = db.query(ChatSession).filter(ChatSession.id == session_id).first()
        assert chat is not None
        assert chat.title == "Sync Test A"
    finally:
        db.refresh(task)
        _cleanup_task(db, task)
        db.close()


def test_ensure_session_reuses_origin_session():
    """Origin-session binding (2026-08-12): a task whose session is an
    origin-chat session (agent_name=None) is REUSED — run output goes to
    the same chat where the task was created. No new dedicated session."""
    db = SessionLocal()
    task = _make_task(db, name="Sync Test B")
    try:
        from app.models.agent_conversation import AgentConversation
        legacy_conv = AgentConversation(
            agent_name=None,
            title="test",
            messages=[],
            status="active",
        )
        db.add(legacy_conv)
        db.flush()
        legacy_chat = ChatSession(
            title="test",
            conversation_id=legacy_conv.id,
            project="global",
        )
        db.add(legacy_chat)
        db.flush()
        task.session_id = legacy_chat.id
        db.commit()
        db.refresh(task)

        from app.routers.automation_api import _ensure_task_chat_session
        same_session_id, created = _ensure_task_chat_session(db, task)
        assert created is False, (
            "Must reuse the origin session, not create a new one"
        )
        assert same_session_id == legacy_chat.id, (
            "Must return the origin session id"
        )
        db.refresh(task)
        assert task.session_id == legacy_chat.id
        still_there = db.query(ChatSession).filter(ChatSession.id == legacy_chat.id).first()
        assert still_there is not None
        assert still_there.title == "test"
    finally:
        db.refresh(task)
        _cleanup_task(db, task)
        db.close()


def test_ensure_session_stamps_created_by_on_new_session():
    """A newly-adopted dedicated session must be OWNED by the task's creator.

    Bug: ``ensure_task_chat_session`` built the ChatSession without
    ``created_by_id`` → the row had NULL owner → invisible in the
    user-scoped sidebar AND unloadable via the owner-scoped ChatSession
    get (so Run Now navigated to a session the UI couldn't show — the
    "chat session not showing / can't move to another chat" report).
    """
    db = SessionLocal()
    task = _make_task(db, name="Owned Session Test")
    task.created_by_id = "user-owner-123"
    db.commit()
    db.refresh(task)
    try:
        from app.routers.automation_api import _ensure_task_chat_session
        session_id, created = _ensure_task_chat_session(db, task)
        assert created is True
        chat = db.query(ChatSession).filter(ChatSession.id == session_id).first()
        assert chat is not None
        assert chat.created_by_id == "user-owner-123", (
            "New dedicated session must be owned by the task's creator so it "
            "appears in their sidebar and is loadable via the owner-scoped API."
        )
    finally:
        db.refresh(task)
        _cleanup_task(db, task)
        db.close()


def test_ensure_session_backfills_created_by_on_matching_session():
    """An existing dedicated session with NULL owner is healed on the happy
    path (title already matches): stamp the task's creator so it becomes
    visible in their sidebar."""
    db = SessionLocal()
    task = _make_task(db, name="Backfill Owner Test")
    task.created_by_id = "user-owner-456"
    db.commit()
    db.refresh(task)
    try:
        from app.models.agent_conversation import AgentConversation
        conv = AgentConversation(
            agent_name=None, title="Backfill Owner Test", messages=[], status="active",
        )
        db.add(conv)
        db.flush()
        chat = ChatSession(
            title="Backfill Owner Test",
            conversation_id=conv.id,
            project="global",
            created_by_id=None,  # orphaned — the bug's signature
            agent_name="automation_agent",
        )
        db.add(chat)
        db.flush()
        task.session_id = chat.id
        db.commit()
        db.refresh(task)

        from app.routers.automation_api import _ensure_task_chat_session
        same_session_id, created = _ensure_task_chat_session(db, task)
        assert created is False  # happy path — title matches
        assert same_session_id == chat.id
        db.refresh(chat)
        assert chat.created_by_id == "user-owner-456", (
            "Orphaned (NULL-owner) dedicated session should be backfilled with "
            "the task's creator on the happy path."
        )
    finally:
        db.refresh(task)
        _cleanup_task(db, task)
        db.close()


def test_ensure_session_noop_when_title_already_matches():
    """A task whose session is already named after it: no-op."""
    db = SessionLocal()
    task = _make_task(db, name="Sync Test C")
    try:
        from app.models.agent_conversation import AgentConversation
        conv = AgentConversation(
            agent_name=None,
            title="Sync Test C",
            messages=[],
            status="active",
        )
        db.add(conv)
        db.flush()
        chat = ChatSession(
            title="Sync Test C",
            conversation_id=conv.id,
            project="global",
            agent_name="automation_agent",
        )
        db.add(chat)
        db.flush()
        task.session_id = chat.id
        db.commit()
        db.refresh(task)

        from app.routers.automation_api import _ensure_task_chat_session
        same_session_id, created = _ensure_task_chat_session(db, task)
        assert created is False
        assert same_session_id == chat.id
    finally:
        db.refresh(task)
        _cleanup_task(db, task)
        db.close()


def test_ensure_session_reuses_old_conversation_for_history():
    """A task whose existing session has ``agent_name='automation_agent'``
    must reuse the OLD AgentConversation so past run results stay visible
    in the per-task chat (Manus UX).

    Pre-condition: a task whose existing dedicated session already links
    to an AgentConversation with some messages in it (simulating prior
    runs). After ensure-session with ``agent_name='automation_agent'``,
    the same ChatSession must point to the SAME AgentConversation so
    those past messages are still visible.
    """
    db = SessionLocal()
    task = _make_task(db, name="Daily Sales Data Sync")
    try:
        from app.models.agent_conversation import AgentConversation
        # Pre-create a dedicated automation session + conversation
        # with a fake past-run message already in it.
        dedicated_conv = AgentConversation(
            agent_name=None,
            title="Daily Sales Data Sync",
            messages=[
                {
                    "role": "assistant",
                    "content": "Run at 2026-07-28 09:11 UTC · execution id abc12345",
                    "meta": {"source": "automation_run"},
                },
            ],
            status="active",
        )
        db.add(dedicated_conv)
        db.flush()
        dedicated_chat = ChatSession(
            title="Daily Sales Data Sync",
            conversation_id=dedicated_conv.id,
            project="global",
            agent_name="automation_agent",
        )
        db.add(dedicated_chat)
        db.flush()
        task.session_id = dedicated_chat.id
        db.commit()
        db.refresh(task)
        old_conv_id = dedicated_conv.id

        from app.routers.automation_api import _ensure_task_chat_session
        same_session_id, created = _ensure_task_chat_session(db, task)
        assert created is False  # already dedicated — no-op
        assert same_session_id == dedicated_chat.id

        # The ChatSession must still point to the old conversation so
        # past run messages remain visible.
        chat = db.query(ChatSession).filter(ChatSession.id == same_session_id).first()
        assert chat is not None
        assert chat.title == "Daily Sales Data Sync"
        assert chat.conversation_id == old_conv_id, (
            "ChatSession should link to the same AgentConversation "
            "so the past run history stays visible."
        )
        assert chat.agent_name == "automation_agent"

        # Verify the conversation still has the past message.
        conv = db.query(AgentConversation).filter(
            AgentConversation.id == old_conv_id,
        ).first()
        assert conv is not None
        assert len(conv.messages) == 1
        assert "abc12345" in conv.messages[0]["content"]
    finally:
        db.refresh(task)
        _cleanup_task(db, task)
        # Best-effort: the legacy chat may also reference the
        # conversation we just deleted; clean it up if so.
        leftover = db.query(ChatSession).filter(
            ChatSession.title == "test",
        ).all()
        for c in leftover:
            db.delete(c)
        db.commit()
        db.close()
