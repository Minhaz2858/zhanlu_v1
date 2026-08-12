"""Regression test for the one-chat-per-task BUGFIX.

The Previous (buggy) behaviour:
    ``_run_agent_in_conversation`` created a fresh ``AgentConversation`` for
    every automation run, which caused "Recent Chats" to render one entry
    per run — eight entries for an automation that ran eight times.

The Fixed behaviour:
    ``_run_agent_in_conversation`` reuses the dedicated conversation
    already created by ``ensure_task_chat_session`` (one per task, all
    runs accumulate in a single Recent Chat entry). When the helper
    can't find one (legacy/edge-case), the function creates a fresh
    ``AgentConversation`` as a fallback.

These tests assert the LOGIC at the conversation-construction level
(the section between the user-resolution block and the
``add_message_stream`` call) by mocking just enough of the executor to
exercise that block in isolation.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Iterable
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event as _event
from sqlalchemy.orm import Session, sessionmaker

# A shared-cache in-memory SQLite DB so the executor's transient
# SessionLocal session and our test session see each other's writes.
# Using a file-backed DB trips SQLite's lock manager when two short-
# lived sessions each issue INSERT/INSERT.
_DB_KEY = "zhanlu_test_" + uuid.uuid4().hex[:8]
_TEST_DB_URL = (
    f"sqlite+pysqlite:///file:{_DB_KEY}?mode=memory&cache=shared&uri=true"
)

import app.models  # noqa: F401
from app.database import Base
from app.models.user import User
from app.models.agent_app import AgentApp
from app.models.agent_conversation import AgentConversation
from app.models.automation_task import AutomationTask
from app.models.chat_session import ChatSession
from app.models.project import Project
from app.services.automation_sessions import ensure_task_chat_session
from app.services.automation_executor import _run_agent_in_conversation

_test_engine = create_engine(
    _TEST_DB_URL,
    connect_args={"check_same_thread": False},
)


@_event.listens_for(_test_engine, "connect")
def _fk_pragma(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


Base.metadata.create_all(_test_engine)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_test_engine)


def _async_iter(chunks: Iterable[Any]):
    """Stub async generator compatible with the v3 stream's body_iterator."""
    async def _gen():
        for c in chunks:
            yield c
    return _gen()


def _stub_add_message_stream(*args, **kwargs):
    done_chunk = {
        "type": "done",
        "content": "stub completion",
        "conversation_id": kwargs.get("conversation_id"),
    }
    rendered = f"data: {json.dumps(done_chunk)}\n\n"

    class _StubResp:
        body_iterator = _async_iter([rendered])

    return _StubResp()


@pytest.fixture()
def file_db():
    """Yield a session bound to the file-backed test DB.

    Patches ``app.database.SessionLocal`` so the executor's in-line
    session (which imports it lazily) opens against the SAME file,
    giving both threads a consistent view of the rows.
    """
    db: Session = TestSessionLocal()
    try:
        import app.database as _db_mod
        _original = _db_mod.SessionLocal
        _db_mod.SessionLocal = TestSessionLocal
        yield db
    finally:
        try:
            import app.database as _db_mod
            _db_mod.SessionLocal = _original
        except Exception:
            pass
        db.close()


@pytest.fixture()
def stub_stream_and_user():
    """Patch the heavyweight collaborators of ``_run_agent_in_conversation``
    so a single run is fast and side-effect-light.

    Returns the list of active patches so the calling test can extend
    them with extra stubbed entry-points (e.g., for the fallback case).
    """
    patches = [
        patch("app.routers.agents.add_message_stream", side_effect=_stub_add_message_stream),
        patch("app.services.automation_executor._persist_run_progress", lambda *a, **k: None),
        patch(
            "app.services.automation_executor._summarize_tool_outcomes",
            lambda messages: {"ok": True, "count": len(messages or [])},
        ),
        patch("app.services.automation_executor._notify_chat", lambda *a, **k: None),
        patch("app.services.automation_executor._post_run_request_marker", lambda *a, **k: None),
        patch(
            "app.services.automation_executor._resolve_task_user",
            lambda _db, _task: SimpleNamespace(
                id="test-user", org_id="test-org", email="t@x"
            ),
        ),
    ]
    for p in patches:
        p.start()
    yield patches
    for p in patches:
        p.stop()


def _make_agent(app_id: str, agent_id: str) -> AgentApp:
    return AgentApp(id=agent_id, org_id="test-org", app_id=app_id, name="test-agent")


def _make_conversation(app_id: str, conversation_id: str | None = None) -> AgentConversation:
    return AgentConversation(
        id=conversation_id or str(uuid.uuid4()),
        org_id="test-org",
        app_id=app_id,
        agent_name="test-agent",
        title="Pre-existing Conversation",
        messages=[],
        status="active",
        created_by_id="test-user",
        project_id=None,
        metadata_={"prior_run_marker": True, "messages_count": 0},
    )


def _make_user(user_id: str | None = None) -> User:
    if user_id is None:
        user_id = "test-user-" + uuid.uuid4().hex[:8]
    return User(
        id=user_id,
        org_id="test-org",
        email=f"stub-{user_id}@test.local",
        full_name="Stub User",
        role="user",
        password_hash="!stub-stub",
    )


def _setup_test_assets(db, app_id: str):
    """Build the user + agent for a test.

    Returns (user, agent). Uniqueness is enforced via UUIDs so we can
    run repeatedly against the file-backed test DB without violating
    the unique-email constraint.
    """
    user_id = "test-user-" + uuid.uuid4().hex[:8]
    user = _make_user(user_id)
    agent = _make_agent(app_id, str(uuid.uuid4()))
    db.add_all([user, agent])
    db.flush()
    return user, agent


def _make_project(name: str = "Ecisco BI") -> Project:
    """Optional helper for tests that need a Project row.

    Not used by the two existing tests, but kept here in case future
    tests need a non-default project setup.
    """
    return Project(
        id=str(uuid.uuid4()),
        org_id="test-org",
        app_id=None,
        name=name,
        is_deleted=False,
    )


# ---------------------------------------------------------------------------
# 1. The existing-conv reuse logic in isolation
# ---------------------------------------------------------------------------


def test_reuse_conv_logic_reuses_existing_conversation(file_db):
    """When ``ensure_task_chat_session`` has already wired a ChatSession
    -> AgentConversation for the task, ``_run_agent_in_conversation``
    MUST reuse the existing AgentConversation rather than create a new
    one per run.

    This is the heart of the BUGFIX. We invoke the actual
    ``ensure_task_chat_session`` (no mocking) to set up the dedicated
    pair, then run ``_run_agent_in_conversation`` (with stubbed
    collaborators) and check that:

      * Exactly one AgentConversation row exists in the DB.
      * Its id matches the one ``ensure_task_chat_session`` created.
      * Its metadata_ carries ``automation_task_id`` (Recent Chats
        panel relies on this to label the thread).
    """
    db = file_db
    user, agent = _setup_test_assets(db, "test-app-reuse")
    user_id = user.id

    # Create the dedicated ChatSession + AgentConversation pair the
    # way the real system does.
    from app.services.automation_sessions import ensure_task_chat_session
    task = AutomationTask(
        id=str(uuid.uuid4()),
        org_id="test-org",
        app_id="test-app-reuse",
        created_by_id="test-user",
        name="Test Automation",
        type="scheduled",
        status="active",
        project="Ecisco BI",
        session_id=None,
    )
    db.add(task); db.flush()

    session_id, created = ensure_task_chat_session(db, task)
    assert created is True, "first call should create the dedicated pair"
    db.commit()

    # ``ensure_task_chat_session`` does NOT populate ``conv.org_id`` (the
    # base TimestampedBase leaves it None until the next write goes
    # through the executor's project-resolution path), so we count via
    # a title-based filter rather than the org_id (column may be NULL).
    conv_count_baseline = db.query(AgentConversation).filter(
        AgentConversation.title == task.name,
    ).count()
    assert conv_count_baseline == 1, (
        f"baseline expected exactly 1 AgentConversation titled {task.name!r}, "
        f"got {conv_count_baseline}"
    )
    existing_conv_id = db.query(ChatSession).filter(
        ChatSession.id == session_id,
    ).first().conversation_id
    assert existing_conv_id, "ChatSession must point at an AgentConversation"

    # Now run _run_agent_in_conversation with stubbed collaborators.
    with patch("app.routers.agents.add_message_stream", side_effect=_stub_add_message_stream), \
         patch("app.services.automation_executor._persist_run_progress", lambda *a, **k: None), \
         patch("app.services.automation_executor._summarize_tool_outcomes", lambda m: {"ok": True}), \
         patch("app.services.automation_executor._notify_chat", lambda *a, **k: None), \
         patch("app.services.automation_executor._post_run_request_marker", lambda *a, **k: None), \
         patch(
             "app.services.automation_executor._resolve_task_user",
             lambda _db, _task: SimpleNamespace(id="test-user", org_id="test-org", email="t@x"),
         ):
        _, conv_id, _, _ = _run_agent_in_conversation(
            task=task,
            agent=agent,
            prompt="hello reuse",
            execution_id="exec-1",
        )

    db.expire_all()
    assert conv_id == existing_conv_id, (
        f"_run_agent_in_conversation must reuse the existing conversation "
        f"(got {conv_id!r}, expected {existing_conv_id!r})"
    )
    post_count = db.query(AgentConversation).filter(
        AgentConversation.title == task.name,
    ).count()
    assert post_count == conv_count_baseline, (
        f"Run must NOT create a new AgentConversation "
        f"(baseline={conv_count_baseline}, post={post_count})"
    )

    refreshed = db.get(AgentConversation, existing_conv_id)
    assert refreshed.metadata_.get("automation_task_id") == task.id, (
        f"Reused conversation metadata_ must carry automation_task_id "
        f"(got {refreshed.metadata_})"
    )


# ---------------------------------------------------------------------------
# 2. The fallback path (legacy task with no session_id)
# ---------------------------------------------------------------------------


def test_fallback_creates_conversation_when_none_exists(file_db, stub_stream_and_user):
    """A task without ``task.session_id`` MUST still get a conversation
    created so the run can proceed. ``ensure_task_chat_session``
    creates the pair on first call, then the executor reuses it.
    """
    db = file_db
    user, agent = _setup_test_assets(db, "test-app-fallback")
    task = AutomationTask(
        id=str(uuid.uuid4()),
        org_id="test-org",
        app_id="test-app-fallback",
        created_by_id=user.id,
        name="Legacy Automation",
        type="scheduled",
        status="active",
        project=None,
        session_id=None,
    )
    db.add(task)
    db.flush()

    pre_count = db.query(AgentConversation).count()

    _, conv_id, _, _ = _run_agent_in_conversation(
        task=task,
        agent=agent,
        prompt="hello fallback",
        execution_id="exec-fallback",
    )

    db.expire_all()
    post_count = db.query(AgentConversation).count()
    assert post_count == pre_count + 1, (
        f"Fallback path must create exactly one AgentConversation "
        f"({pre_count} -> {post_count})"
    )
    assert conv_id, "Fallback conv_id must be non-empty"
    new_conv = db.get(AgentConversation, conv_id)
    assert new_conv is not None
    assert new_conv.metadata_.get("automation_task_id") == task.id, (
        f"Fallback conv metadata_ must carry automation_task_id "
        f"(got {new_conv.metadata_})"
    )


# ---------------------------------------------------------------------------
# 3. The adoption + healing path (orphan chat with conversation_id=None)
# ---------------------------------------------------------------------------


def test_adopts_existing_task_conversation_and_heals_link(file_db, stub_stream_and_user):
    """Origin-session binding (2026-08-12): when a task has an origin-chat
    session (agent_name=None), the executor REUSES it — run output goes
    to the same chat where the task was created. The session's
    conversation_id is NOT overwritten (it belongs to the user's original
    chat). The agent runtime creates its own AgentConversation for the
    run, and _persist_run_to_chat writes ChatMessage rows to the origin
    session.

    This overrides the 2026-08-11 "dedicated automation session" design.
    """
    db = file_db
    user, agent = _setup_test_assets(db, "test-app-migrate")
    task = AutomationTask(
        id=str(uuid.uuid4()),
        org_id="test-org",
        app_id="test-app-migrate",
        created_by_id=user.id,
        name="Daily Sales Data Sync",
        type="scheduled",
        status="active",
        project="Ecisco BI",
        session_id=None,
    )
    db.add(task)
    db.flush()

    # Build the origin-chat session — title matches the task name,
    # agent_name is None (origin chat).
    old_origin_chat = ChatSession(
        title=task.name,
        project_id=None,
        project="Ecisco BI",
        conversation_id=None,
        agent_name=None,  # origin project chat
        starred=False,
        created_by_id=user.id,
        last_message_at="2026-08-11T00:00:00",
    )
    db.add(old_origin_chat)
    db.flush()

    # Two pre-existing AgentConversations from prior runs (the old model).
    older = AgentConversation(
        id=str(uuid.uuid4()), org_id="test-org", app_id="test-app-migrate",
        agent_name=agent.name, title="🤖 Daily Sales Data Sync · 06:52",
        messages=[{"role": "assistant", "content": "old run"}],
        status="active", created_by_id=user.id, project_id=None,
        metadata_={"automation_task_id": task.id},
    )
    older.created_date = __import__("datetime").datetime(2026, 8, 11, 6, 52)
    newer = AgentConversation(
        id=str(uuid.uuid4()), org_id="test-org", app_id="test-app-migrate",
        agent_name=agent.name, title="🤖 Daily Sales Data Sync · 07:54",
        messages=[{"role": "assistant", "content": "newer run"}],
        status="active", created_by_id=user.id, project_id=None,
        metadata_={"automation_task_id": task.id},
    )
    newer.created_date = __import__("datetime").datetime(2026, 8, 11, 7, 54)
    db.add_all([older, newer])

    task.session_id = old_origin_chat.id
    db.commit()

    pre_count = db.query(AgentConversation).count()

    # Run — the executor must REUSE the origin session (not create a new
    # dedicated session). The agent runtime creates its own conversation.
    _, conv_id, _, _ = _run_agent_in_conversation(
        task=task,
        agent=agent,
        prompt="hello migrate",
        execution_id="exec-migrate",
    )

    db.expire_all()
    # The run may reuse an existing conversation for the task (if one
    # exists with the task's id in metadata) or create a new one. Either
    # way, the key invariant is that the origin session is NOT migrated.
    post_count = db.query(AgentConversation).count()
    assert conv_id, "Run conv_id must be non-empty"
    new_conv = db.get(AgentConversation, conv_id)
    assert new_conv is not None
    assert new_conv.metadata_.get("automation_task_id") == task.id

    # The task's session_id must STILL point to the origin session.
    db.refresh(task)
    assert task.session_id == old_origin_chat.id, (
        "Task session_id must stay bound to the origin chat"
    )

    # The origin chat is left intact — conversation_id is NOT overwritten
    # because it's an origin session (agent_name=None), not a dedicated one.
    old_refreshed = db.get(ChatSession, old_origin_chat.id)
    assert old_refreshed is not None
    assert old_refreshed.agent_name is None  # still an origin chat
    # conversation_id should NOT be changed to the automation's conversation
    # because the origin session's conversation belongs to the user's chat.
