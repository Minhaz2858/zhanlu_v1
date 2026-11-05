"""Stage-1 FSM persistence correction (2026-10-30) — unit tests.

Invariant under test: the FSM route's conv.messages persist must leave the
conversation with the user message AND the assistant answer present exactly
once, agreeing with the streamed content (no truncation), with regenerate
replacing (not stacking) the previous assistant reply.
"""

import uuid

import pytest

from app.routers.agents import _merge_fsm_conv_messages, _persist_fsm_turn


def _user_msg(content="give me a sales report", mid=None):
    return {
        "id": mid or str(uuid.uuid4()),
        "role": "user",
        "content": content,
        "created_date": "2026-10-30T00:00:00+00:00",
    }


def _assistant_msg(content="## Sales Report\n...", mid=None):
    return {
        "id": mid or str(uuid.uuid4()),
        "role": "assistant",
        "content": content,
        "created_date": "2026-10-30T00:00:01+00:00",
    }


class TestMergeFsmConvMessages:
    def test_normal_turn_keeps_user_and_appends_assistant(self):
        user = _user_msg()
        existing = [
            {"id": "old-a", "role": "assistant", "content": "hi"},
            user,
        ]
        assistant = _assistant_msg()
        merged = _merge_fsm_conv_messages(existing, user, assistant)
        assert [m["id"] for m in merged] == ["old-a", user["id"], assistant["id"]]

    def test_missing_user_message_repaired(self):
        # Early user-message commit failed: the fresh DB row lacks it.
        user = _user_msg()
        existing = [{"id": "old-a", "role": "assistant", "content": "hi"}]
        assistant = _assistant_msg()
        merged = _merge_fsm_conv_messages(existing, user, assistant)
        assert [m["id"] for m in merged] == ["old-a", user["id"], assistant["id"]]
        assert merged[1]["role"] == "user"

    def test_empty_existing_yields_user_then_assistant(self):
        user = _user_msg()
        assistant = _assistant_msg()
        merged = _merge_fsm_conv_messages([], user, assistant)
        assert [m["id"] for m in merged] == [user["id"], assistant["id"]]

    def test_regenerate_replaces_trailing_assistant(self):
        user = _user_msg()
        old_assistant = _assistant_msg(content="old answer")
        existing = [user, old_assistant]
        new_assistant = _assistant_msg(content="new answer")
        merged = _merge_fsm_conv_messages(
            existing, user, new_assistant, regenerate=True
        )
        assert [m["id"] for m in merged] == [user["id"], new_assistant["id"]]
        assert merged[-1]["content"] == "new answer"

    def test_regenerate_does_not_duplicate_user_message(self):
        user = _user_msg()
        existing = [user]
        new_assistant = _assistant_msg()
        merged = _merge_fsm_conv_messages(
            existing, user, new_assistant, regenerate=True
        )
        user_bubbles = [m for m in merged if m.get("role") == "user"]
        assert len(user_bubbles) == 1
        assert user_bubbles[0]["id"] == user["id"]

    def test_idempotent_no_duplicate_assistant_on_retry(self):
        user = _user_msg()
        assistant = _assistant_msg()
        once = _merge_fsm_conv_messages([user], user, assistant)
        twice = _merge_fsm_conv_messages(once, user, assistant)
        assert [m["id"] for m in twice] == [user["id"], assistant["id"]]

    def test_non_dict_entries_preserved(self):
        user = _user_msg()
        sentinel = "legacy-string-entry"
        existing = [sentinel, user]
        assistant = _assistant_msg()
        merged = _merge_fsm_conv_messages(existing, user, assistant)
        assert merged[0] is sentinel
        assert [m["id"] for m in merged[1:]] == [user["id"], assistant["id"]]

    def test_regenerate_stale_trailing_assistant_only_when_last(self):
        # An assistant reply NOT at the tail (e.g. followed by another user
        # message) must not be popped on regenerate — only the trailing one.
        user = _user_msg()
        prior_assistant = _assistant_msg(content="turn-1 answer")
        mid_user = _user_msg(content="follow-up")
        existing = [user, prior_assistant, mid_user]
        new_assistant = _assistant_msg()
        merged = _merge_fsm_conv_messages(
            existing, None, new_assistant, regenerate=True
        )
        assert prior_assistant in merged
        assert merged[-1]["id"] == new_assistant["id"]

    def test_user_msg_none_appends_assistant_only(self):
        assistant = _assistant_msg()
        merged = _merge_fsm_conv_messages([], None, assistant)
        assert [m["id"] for m in merged] == [assistant["id"]]

    def test_input_list_not_mutated(self):
        user = _user_msg()
        existing = [user]
        assistant = _assistant_msg()
        snapshot = list(existing)
        _merge_fsm_conv_messages(existing, user, assistant)
        assert existing == snapshot

    def test_assistant_content_not_truncated(self):
        long_content = "x" * 5000
        user = _user_msg()
        assistant = _assistant_msg(content=long_content)
        merged = _merge_fsm_conv_messages([user], user, assistant)
        assert merged[-1]["content"] == long_content
        assert len(merged[-1]["content"]) == 5000

    def test_regenerate_without_trailing_assistant_appends(self):
        user = _user_msg()
        existing = [user]
        new_assistant = _assistant_msg()
        merged = _merge_fsm_conv_messages(
            existing, user, new_assistant, regenerate=True
        )
        assert [m["id"] for m in merged] == [user["id"], new_assistant["id"]]


# ── DB-level verification (Stage-1 acceptance review, 2026-10-30) ──────
# The user asked: does the missing-user-message recovery restore the
# normalized `chat_messages` table too? Answer encoded as tests:
#   - `chat_messages` user rows are CLIENT-created (entity API) in
#     interactive chat — the backend never writes user rows there (by
#     design; see _persist_fsm_turn docstring).
#   - The backend's transaction covers: assistant `chat_messages` row +
#     `agent_conversations.messages` (with user repair) — ATOMICALLY.
# These tests use REAL models against SQLite to verify the transaction
# boundary, including rollback (neither store changes on commit failure).

from sqlalchemy import create_engine, event, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.routers.agents as agents_module
from app.models.agent_conversation import AgentConversation
from app.models.chat_message import ChatMessage


@pytest.fixture()
def db_env():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Shared single connection so every session sees the same in-memory DB.
    @event.listens_for(engine, "connect")
    def _do_connect(dbapi_connection, connection_record):
        pass  # StaticPool already guarantees one connection

    AgentConversation.metadata.create_all(
        engine, tables=[AgentConversation.__table__, ChatMessage.__table__]
    )
    maker = sessionmaker(bind=engine)
    db = maker()
    conv = AgentConversation(
        id="conv-1",
        agent_name="test_agent",
        title="t",
        messages=[
            {"id": "prior-a", "role": "assistant", "content": "prior reply"},
        ],
    )
    db.add(conv)
    db.commit()
    yield {"engine": engine, "maker": maker, "db": db}
    db.close()
    engine.dispose()


def _persist(env, **overrides):
    defaults = dict(
        chat_session_id="sess-1",
        regenerate=False,
        assistant_content="## Report\nfull answer",
        plan_summary={"nodes": []},
        live_events=[],
        citations=[{"source_id": "ds1", "source_name": "Warehouse", "rows": 10}],
        user_msg=_user_msg(),
        assistant_msg=_assistant_msg(),
    )
    defaults.update(overrides)
    return _persist_fsm_turn(env["engine"], "conv-1", **defaults)


@pytest.mark.usefixtures("db_env")
class TestPersistFsmTurnDbLevel:
    def test_both_stores_persisted_atomically(self, db_env):
        user, assistant = _user_msg(), _assistant_msg()
        result = _persist(db_env, user_msg=user, assistant_msg=assistant)
        # conv.messages: prior + user + assistant, in order, full content.
        # D4: the result is now {"messages", "assistant_message_id",
        # "answer_persisted"} — exact-row finalization metadata.
        assert result["answer_persisted"] is True
        # No client_message_id transported here → legacy INSERT fallback:
        # the attested id is the freshly inserted row's id.
        assert result["assistant_message_id"] != assistant["id"]
        assert [m["id"] for m in result["messages"]] == [
            "prior-a", user["id"], assistant["id"],
        ]
        # Fresh-session read-back — what a reload would see.
        db = db_env["maker"]()
        try:
            conv = db.get(AgentConversation, "conv-1")
            assert [m["id"] for m in conv.messages] == [
                "prior-a", user["id"], assistant["id"],
            ]
            rows = db.scalars(
                select(ChatMessage).where(ChatMessage.session_id == "sess-1")
            ).all()
            assert len(rows) == 1
            assert rows[0].role == "assistant"
            assert rows[0].content == "## Report\nfull answer"
            assert rows[0].sources == [
                {"source_id": "ds1", "source_name": "Warehouse", "rows": 10}
            ]
        finally:
            db.close()

    def test_missing_user_message_repaired_in_conv_store(self, db_env):
        # Early user-message commit failed → conv row lacks it. The repair
        # must land it alongside the assistant answer in the SAME commit.
        user, assistant = _user_msg(), _assistant_msg()
        db = db_env["maker"]()
        try:
            conv = db.get(AgentConversation, "conv-1")
            assert all(m["id"] != user["id"] for m in conv.messages)
        finally:
            db.close()
        _persist(db_env, user_msg=user, assistant_msg=assistant)
        db = db_env["maker"]()
        try:
            conv = db.get(AgentConversation, "conv-1")
            ids = [m["id"] for m in conv.messages]
            assert user["id"] in ids and assistant["id"] in ids
        finally:
            db.close()

    def test_atomic_rollback_neither_store_changes(self, db_env, monkeypatch):
        # Force a REAL commit failure: the assistant chat_messages row gets a
        # PK colliding with a pre-existing row → IntegrityError at flush.
        # Atomicity requires that conv.messages ALSO does not change.
        db = db_env["maker"]()
        try:
            db.add(ChatMessage(
                id="collision-id", session_id="sess-1",
                role="assistant", content="pre-existing",
            ))
            db.commit()
            conv_before = list(db.get(AgentConversation, "conv-1").messages)
        finally:
            db.close()
        monkeypatch.setattr(
            agents_module.uuid, "uuid4", lambda: "collision-id"
        )
        with pytest.raises(Exception):
            _persist(db_env)
        db = db_env["maker"]()
        try:
            count = db.scalar(
                select(func.count()).select_from(ChatMessage).where(
                    ChatMessage.session_id == "sess-1"
                )
            )
            assert count == 1  # only the pre-existing row; insert rolled back
            conv_after = list(db.get(AgentConversation, "conv-1").messages)
            assert conv_after == conv_before  # conv.messages untouched too
        finally:
            db.close()

    def test_regenerate_skips_chat_message_row_and_replaces(self, db_env):
        user = _user_msg()
        old_assistant = _assistant_msg(content="old answer")
        # Seed the conversation as [user, old_assistant].
        db = db_env["maker"]()
        try:
            conv = db.get(AgentConversation, "conv-1")
            conv.messages = [user, old_assistant]
            db.commit()
        finally:
            db.close()
        new_assistant = _assistant_msg(content="new answer")
        result = _persist(
            db_env, user_msg=user, assistant_msg=new_assistant,
            regenerate=True,
        )
        assert [m["id"] for m in result["messages"]] == [user["id"], new_assistant["id"]]
        # Regenerate: the client row is the writer of record — no backend
        # persistence attestation.
        assert result["answer_persisted"] is False
        db = db_env["maker"]()
        try:
            rows = db.scalars(
                select(ChatMessage).where(ChatMessage.session_id == "sess-1")
            ).all()
            assert rows == []  # regenerate: client's optimistic row is writer of record
            conv = db.get(AgentConversation, "conv-1")
            assert conv.messages[-1]["content"] == "new answer"
        finally:
            db.close()

    def test_full_content_no_truncation_in_both_stores(self, db_env):
        long_content = "y" * 5000
        assistant = _assistant_msg(content=long_content)
        # In the route both args come from the SAME streamed assistant_content.
        _persist(db_env, assistant_content=long_content, assistant_msg=assistant)
        db = db_env["maker"]()
        try:
            row = db.scalar(
                select(ChatMessage).where(ChatMessage.session_id == "sess-1")
            )
            assert len(row.content) == 5000
            conv = db.get(AgentConversation, "conv-1")
            assert len(conv.messages[-1]["content"]) == 5000
            # Stream/persist agreement: identical text in both stores.
            assert row.content == conv.messages[-1]["content"]
        finally:
            db.close()

    def test_missing_conversation_returns_none(self, db_env):
        result = _persist_fsm_turn(
            db_env["engine"], "conv-does-not-exist",
            chat_session_id="sess-1", regenerate=False,
            assistant_content="x", user_msg=_user_msg(),
            assistant_msg=_assistant_msg(),
        )
        assert result is None

    def test_user_message_never_written_to_chat_messages_table(self, db_env):
        # Encodes the dual-store contract: the backend writes NO user rows
        # to `chat_messages` — user rows there are client-created.
        user = _user_msg()
        _persist(db_env, user_msg=user)
        db = db_env["maker"]()
        try:
            user_rows = db.scalars(
                select(ChatMessage).where(ChatMessage.role == "user")
            ).all()
            assert user_rows == []
        finally:
            db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
