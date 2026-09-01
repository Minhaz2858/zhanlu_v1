"""
Test automation session behaviour (origin-session binding, 2026-08-12).

Coverage:
  - ensure_task_chat_session reuses origin sessions (agent_name=None)
  - New fallback sessions are tagged with agent_name='automation_agent'
  - Existing dedicated sessions (agent_name='automation_agent') are still reused
  - NO-PREAMBLE directive is in the executor prompt
  - _HONESTY_GUARDRAIL no longer contains the old verbose "say so explicitly and stop"
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event as _event
from sqlalchemy.orm import Session, sessionmaker

from app.models.base import Base
from app.models.automation_task import AutomationTask
from app.models.chat_session import ChatSession
from app.models.agent_conversation import AgentConversation

# ── File-backed shared-cache SQLite (same pattern as test_automation_one_chat_per_task.py) ──
_DB_KEY = "zhanlu_test_route_" + uuid.uuid4().hex[:8]
_TEST_DB_URL = (
    f"sqlite+pysqlite:///file:{_DB_KEY}?mode=memory&cache=shared&uri=true"
)

_test_engine = create_engine(
    _TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool,
)

@_event.listens_for(_test_engine, "connect")
def _enable_fk(dbapi_conn, _rec):
    c = dbapi_conn.cursor()
    c.execute("PRAGMA foreign_keys=ON")
    c.close()

Base.metadata.create_all(_test_engine)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_test_engine)


@pytest.fixture()
def file_db():
    """Yield a session bound to the file-backed test DB, patching
    app.database.SessionLocal so the service-layer calls use the same DB."""
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


def _make_task(db, name="Test Auto", project="TestPj"):
    task = AutomationTask(
        id=str(uuid.uuid4()),
        org_id="test-org",
        app_id="test-app",
        created_by_id="test-user",
        name=name,
        type="scheduled",
        status="active",
        project=project,
        agent_id=None,
        session_id=None,
    )
    db.add(task)
    db.flush()
    return task


# ── Tests ────────────────────────────────────────────────────────────


class TestDedicatedSessionAgentName:

    def test_new_fallback_session_has_agent_name_automation_agent(self, file_db):
        """When a task has no session at all, ensure_task_chat_session creates
        a new fallback session tagged with agent_name='automation_agent'."""
        from app.services.automation_sessions import ensure_task_chat_session

        db = file_db
        task = _make_task(db, "Daily Data Sync")
        sid, created = ensure_task_chat_session(db, task)
        assert created is True
        chat = db.query(ChatSession).filter(ChatSession.id == sid).first()
        assert chat is not None
        assert chat.agent_name == "automation_agent", (
            f"Expected agent_name='automation_agent', got {chat.agent_name!r}"
        )

    def test_reuse_when_already_dedicated(self, file_db):
        """If the task already has a dedicated session, ensure_task_chat_session
        must return it with created=False (no-op)."""
        from app.services.automation_sessions import ensure_task_chat_session

        db = file_db
        task = _make_task(db, "Daily Data Sync")
        sid1, created1 = ensure_task_chat_session(db, task)
        assert created1 is True
        sid2, created2 = ensure_task_chat_session(db, task)
        assert created2 is False
        assert sid2 == sid1, (
            "Second call must reuse the same dedicated session"
        )

    def test_origin_session_binding_reuses_origin(self, file_db):
        """Origin-session binding (2026-08-12): when task.session_id points
        at an origin-chat session (agent_name=None), ensure_task_chat_session
        REUSES it — run output goes to the same chat where the task was
        created. No new dedicated session is created."""
        from app.services.automation_sessions import ensure_task_chat_session

        db = file_db
        task = _make_task(db, "Migrate Test")
        # Simulate an origin-chat session.
        origin_chat = ChatSession(
            title=task.name,
            conversation_id=None,
            project="TestPj",
            agent_name=None,  # origin project chat
        )
        db.add(origin_chat)
        db.flush()
        task.session_id = origin_chat.id
        db.commit()
        db.refresh(task)

        sid, created = ensure_task_chat_session(db, task)
        assert created is False, (
            "Must reuse the origin session, not create a new one"
        )
        assert sid == origin_chat.id, (
            "Must return the origin session id, not create a new one"
        )
        db.refresh(task)
        assert task.session_id == origin_chat.id, (
            "Task session_id must stay bound to the origin chat"
        )

        # Origin chat is left intact.
        origin_refreshed = db.get(ChatSession, origin_chat.id)
        assert origin_refreshed is not None
        assert origin_refreshed.agent_name is None


class TestExecutorPrompt:

    def test_no_preamble_directive_exists(self):
        """_NO_PREAMBLE must instruct the agent to produce the
        deliverable directly without boundary narration."""
        from app.services.automation_executor import _NO_PREAMBLE
        assert _NO_PREAMBLE
        assert "Produce the deliverable directly" in _NO_PREAMBLE
        assert "Do not narrate your boundaries" in _NO_PREAMBLE

    def test_honesty_guardrail_softened(self):
        """_HONESTY_GUARDRAIL must not contain the old self-referential
        'stop' narration that caused verbose preambles."""
        from app.services.automation_executor import _HONESTY_GUARDRAIL
        assert _HONESTY_GUARDRAIL
        # Old phrasing "say so explicitly and stop" is removed.
        assert "say so explicitly and stop" not in _HONESTY_GUARDRAIL
        # New phrasing is concise.
        assert "state the issue concisely" in _HONESTY_GUARDRAIL


class TestNotifyChatRemoved:

    def test_execute_automation_success_path_has_no_notify_chat(self):
        """The success path of execute_automation must NOT call _notify_chat
        — the agent's own response is the deliverable (2026-08-11 cleanup)."""
        import inspect
        from app.services import automation_executor as sut

        source = inspect.getsource(sut.execute_automation)
        # The _notify_chat function should only appear in the function
        # definition (def _notify_chat) and failure paths (def _notify_chat_failure).
        # Verify it's not called in the success completion section.
        # The "Final CAS: running -> completed" comment marks the start of
        # the success completion section.
        completion_idx = source.find("Final CAS")
        if completion_idx >= 0:
            after_completion = source[completion_idx:]
            assert "_notify_chat(" not in after_completion, (
                "_notify_chat must not be called in the success completion path "
                "of execute_automation"
            )
