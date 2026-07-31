"""Regression: ``ChatSession`` must actually persist the ``conversation_id``
and ``agent_name`` columns the frontend writes via the generic entity
API. The entity service filters the update payload to the model's
declared columns and silently drops anything else — so the moment
these two columns are missing from the model (or the underlying DB
table), the write succeeds, returns 200, and the link is lost on the
next reload.

Why this exists
---------------
A user reported that reopening a ChatSession in the sidebar never
auto-restored the agent they had selected, and that the Recent Chats
list on the Project Detail page grew one row per user message. The
root cause was a half-built feature:

  * The frontend (``Chat.jsx``) wrote
    ``{conversation_id, agent_name}`` to ``ChatSession`` on every
    message.
  * The generic entity service silently dropped both keys because the
    ``ChatSession`` model didn't declare them — and no migration had
    added them to the underlying table.
  * On reload, ``session.conversation_id`` was ``None`` and the resume
    path created a brand-new ``AgentConversation`` per message.

These tests pin the fix from both ends:

  1. **Model layer** — the model declares both columns (a future
     refactor that removes them again fails here).
  2. **Entity-service layer** — a write of ``{conversation_id,
     agent_name}`` actually persists, both on create and on update
     (a future refactor that re-introduces the silent-drop bug fails
     here).
  3. **FK layer** — setting ``conversation_id`` to a value that
     doesn't reference a real ``AgentConversation.id`` is rejected,
     because the column is a real FK (a future refactor that drops
     the FK in favour of a plain string fails here).

The test uses an in-memory SQLite with the full model registry, so it
exercises the same code path the real backend uses on ``PUT
/entities/chat-sessions/{id}``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

from app.database import Base
from app.models import ChatSession, AgentConversation
from app.services.entity_service import (
    create_record,
    update_record,
    get_record,
)


# ── Fixtures ─────────────────────────────────────────────────────────


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


def _make_session(db, *, title="Test Chat"):
    """Helper: insert a minimal ChatSession and return it as a dict."""
    return create_record(
        ChatSession, {"title": title}, db
    )


def _make_agent_conversation(db, *, agent_name="general_assistant"):
    """Helper: insert a minimal AgentConversation and return its id."""
    conv = create_record(
        AgentConversation,
        {"agent_name": agent_name, "messages": [], "status": "active"},
        db,
    )
    return conv["id"]


# ── Model-level assertions ───────────────────────────────────────────


class TestChatSessionModel:
    """The columns must be declared on the model so the entity
    service's _filter_data whitelist accepts them."""

    def test_conversation_id_column_exists(self):
        cols = {c.name for c in ChatSession.__table__.columns}
        assert "conversation_id" in cols, (
            "ChatSession is missing the 'conversation_id' column. "
            "Without it the generic entity service silently drops "
            "writes of conversation_id, so the ChatSession → "
            "AgentConversation link is never persisted."
        )

    def test_agent_name_column_exists(self):
        cols = {c.name for c in ChatSession.__table__.columns}
        assert "agent_name" in cols, (
            "ChatSession is missing the 'agent_name' column. Without "
            "it the sidebar cannot show which agent a session was "
            "opened with."
        )


# ── Entity-service assertions ────────────────────────────────────────


class TestChatSessionConversationLinkPersist:
    """The end-to-end contract the frontend relies on."""

    def test_update_with_conversation_id_and_agent_name_persists(
        self, db
    ):
        """The exact write Chat.jsx makes on every message must round-trip."""
        sess = _make_session(db)
        conv_id = _make_agent_conversation(db)

        result = update_record(
            ChatSession,
            sess["id"],
            {
                "conversation_id": conv_id,
                "agent_name": "general_assistant",
            },
            db,
        )
        assert result is not None, "update_record returned None"
        assert result["conversation_id"] == conv_id, (
            f"conversation_id not persisted: got {result.get('conversation_id')!r}, "
            f"expected {conv_id!r}. This is the silent-drop bug: "
            f"entity_service._filter_data stripped the key because "
            f"the model didn't declare the column."
        )
        assert result["agent_name"] == "general_assistant", (
            f"agent_name not persisted: got {result.get('agent_name')!r}"
        )

    def test_update_persists_conversation_id_only(self, db):
        """Updating just conversation_id (no agent_name) must work."""
        sess = _make_session(db)
        conv_id = _make_agent_conversation(db)

        result = update_record(
            ChatSession,
            sess["id"],
            {"conversation_id": conv_id},
            db,
        )
        assert result["conversation_id"] == conv_id
        # agent_name was not in this update — it stays at the default
        # value (None) and must NOT have been clobbered to something
        # weird.
        assert result.get("agent_name") in (None, "")

    def test_update_persists_agent_name_only(self, db):
        """Updating just agent_name (no conversation_id) must work."""
        sess = _make_session(db)

        result = update_record(
            ChatSession,
            sess["id"],
            {"agent_name": "data_analyst"},
            db,
        )
        assert result["agent_name"] == "data_analyst"
        # conversation_id stays unset
        assert result.get("conversation_id") is None

    def test_subsequent_update_reuses_conversation_id(self, db):
        """The whole point of the feature: a second message in the
        same session reuses the SAME AgentConversation, not a new one.
        We simulate this by reading the row back after update and
        confirming the conversation_id is still set."""
        sess = _make_session(db)
        first_conv = _make_agent_conversation(db)

        update_record(
            ChatSession,
            sess["id"],
            {"conversation_id": first_conv, "agent_name": "general_assistant"},
            db,
        )

        # Simulate a reload: read the session back through the
        # entity-service getter (same code path Chat.jsx's
        # ``getSession(sid)`` goes through).
        reloaded = get_record(ChatSession, sess["id"], db)
        assert reloaded is not None
        assert reloaded["conversation_id"] == first_conv, (
            "After a reload, the ChatSession's conversation_id is "
            "wrong. The frontend would fall through to "
            "createAgentConversation() and create a brand-new row."
        )
        assert reloaded["agent_name"] == "general_assistant"

    def test_can_overwrite_conversation_id(self, db):
        """A session that's been "moved" to a new conv must accept the
        update without raising. (The old conv row stays orphaned,
        which matches the existing behaviour.)"""
        sess = _make_session(db)
        conv_a = _make_agent_conversation(db, agent_name="a")
        conv_b = _make_agent_conversation(db, agent_name="b")

        update_record(
            ChatSession,
            sess["id"],
            {"conversation_id": conv_a, "agent_name": "a"},
            db,
        )
        result = update_record(
            ChatSession,
            sess["id"],
            {"conversation_id": conv_b, "agent_name": "b"},
            db,
        )
        assert result["conversation_id"] == conv_b
        assert result["agent_name"] == "b"

    def test_can_clear_conversation_id_with_null(self, db):
        """``handleNewChat`` resets the session — the frontend expects
        to be able to write ``conversation_id: null`` and have it
        round-trip."""
        sess = _make_session(db)
        conv = _make_agent_conversation(db)
        update_record(
            ChatSession,
            sess["id"],
            {"conversation_id": conv},
            db,
        )

        result = update_record(
            ChatSession,
            sess["id"],
            {"conversation_id": None},
            db,
        )
        assert result["conversation_id"] is None


# ── FK-level assertions ─────────────────────────────────────────────


class TestChatSessionConversationFK:
    """conversation_id must be a real FK so dangling refs are
    rejected at the DB layer. (If a future migration drops the FK
    in favour of a plain string column, these tests fail.)"""

    def test_creating_with_unknown_conversation_id_fails(self, db):
        """An unrelated UUID should be rejected because the FK to
        agent_conversations.id requires the target row to exist.

        SQLite (used here) is permissive about FK enforcement unless
        we explicitly enable it — but the production config in
        ``app.database`` does enable ``PRAGMA foreign_keys=ON``. We
        simulate that on the in-memory engine too."""
        # NOTE: we deliberately create the engine *with* FK
        # enforcement, mirroring production. The fixture's
        # ``create_engine`` defaults to no PRAGMA, so re-do it here.
        from sqlalchemy import event
        engine = create_engine("sqlite:///:memory:")
        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
        Base.metadata.create_all(engine)
        Sess = sessionmaker(bind=engine)
        db = Sess()
        try:
            with pytest.raises(IntegrityError):
                create_record(
                    ChatSession,
                    {
                        "title": "bad",
                        "conversation_id": "deadbeef-dead-beef-dead-beefdeadbeef",
                    },
                    db,
                )
        finally:
            db.close()
            engine.dispose()

    def test_creating_with_real_conversation_id_succeeds(self, db):
        """Sanity check the inverse: a real conv id is accepted."""
        conv_id = _make_agent_conversation(db)
        result = create_record(
            ChatSession,
            {
                "title": "linked",
                "conversation_id": conv_id,
                "agent_name": "general_assistant",
            },
            db,
        )
        assert result["conversation_id"] == conv_id
        assert result["agent_name"] == "general_assistant"