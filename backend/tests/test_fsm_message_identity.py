"""D4 — authoritative chat message identity for the FSM route (TDD).

Contract (Phase 2 of the lifecycle repair):
  The client creates the assistant ``chat_messages`` row BEFORE streaming and
  transports its id as ``assistant_message_id`` (already validated + recorded
  by Stage 2B Commit 4A). Backend persistence must RECONCILE with that
  identity — update the existing row in place — instead of INSERTing a second
  row with a fresh uuid (the confirmed duplicate/twin defect). INSERT remains
  the fallback for old clients that transport no identity, and when the
  referenced row is missing or foreign (tenant/session guard).

Deterministic: sqlite in-memory, no LLM, no network.
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.agent_conversation import AgentConversation
from app.models.chat_message import ChatMessage as Message
from app.models.chat_session import ChatSession


@pytest.fixture()
def db_bind():
    engine = create_engine("sqlite://")
    ChatSession.__table__.create(engine)
    Message.__table__.create(engine)
    AgentConversation.__table__.create(engine)
    return engine


@pytest.fixture()
def db(db_bind):
    s = sessionmaker(bind=db_bind)()
    s.add(ChatSession(id="sess-1", conversation_id="conv-1", title="t"))
    s.add(AgentConversation(id="conv-1", agent_name="general_assistant",
                            messages=[]))
    # The client-created optimistic assistant row (empty content) — the ONLY
    # id backed by a real row at stream time.
    s.add(Message(id="msg-client-1", session_id="sess-1", role="assistant",
                  content="", order=1))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _persist(db_bind, **overrides):
    from app.routers.agents import _persist_fsm_turn

    kwargs = dict(
        chat_session_id="sess-1",
        regenerate=False,
        assistant_content="the answer",
        user_msg={"id": "msg-user-1", "role": "user", "content": "hi"},
        assistant_msg={"id": "msg-client-1", "role": "assistant",
                       "content": "the answer"},
        client_message_id="msg-client-1",
    )
    kwargs.update(overrides)
    return _persist_fsm_turn(db_bind, "conv-1", **kwargs)


def _assistant_rows(s):
    return (
        s.query(Message)
        .filter(Message.role == "assistant", Message.is_deleted == False)  # noqa: E712
        .order_by(Message.order)
        .all()
    )


# ── A. FSM: one turn → one row, identity reconciled ─────────────────────────
def test_fsm_persists_into_the_client_row(db, db_bind):
    result = _persist(db_bind)
    rows = _assistant_rows(db)
    assert len(rows) == 1, "client row + backend INSERT = the twin defect"
    assert rows[0].id == "msg-client-1"
    assert rows[0].content == "the answer"
    assert result["assistant_message_id"] == "msg-client-1"
    assert result["answer_persisted"] is True
    # conv.messages carries the SAME authoritative identity.
    assistant_entries = [m for m in result["messages"]
                         if m.get("role") == "assistant"]
    assert [m["id"] for m in assistant_entries] == ["msg-client-1"]


def test_fsm_repeated_persist_is_idempotent(db, db_bind):
    _persist(db_bind)
    _persist(db_bind)
    rows = _assistant_rows(db)
    assert len(rows) == 1
    assert rows[0].content == "the answer"


def test_fsm_no_client_id_falls_back_to_insert(db, db_bind):
    """Old clients that transport no identity keep the legacy behavior."""
    result = _persist(db_bind, client_message_id=None,
                      assistant_msg={"id": "msg-backend-1", "role": "assistant",
                                     "content": "the answer"})
    filled = [r for r in _assistant_rows(db) if (r.content or "").strip()]
    assert len(filled) == 1
    assert filled[0].id != "msg-client-1"
    assert filled[0].content == "the answer"
    assert result["assistant_message_id"] == filled[0].id
    assert result["answer_persisted"] is True


def test_fsm_missing_client_row_falls_back_to_insert(db, db_bind):
    result = _persist(db_bind, client_message_id="msg-does-not-exist",
                      assistant_msg={"id": "msg-backend-2", "role": "assistant",
                                     "content": "the answer"})
    filled = [r for r in _assistant_rows(db) if (r.content or "").strip()]
    assert len(filled) == 1
    # Legacy fallback: the inserted row keeps a backend uuid (the client had
    # no real row) — result attests which id won; conv.messages carries the
    # assistant_msg id (documented legacy split for identity-less clients).
    assert result["assistant_message_id"] == filled[0].id
    conv_assistant = [m for m in result["messages"] if m.get("role") == "assistant"]
    assert conv_assistant[-1]["id"] == "msg-backend-2"
    assert all(r.id != "msg-does-not-exist" for r in _assistant_rows(db))


def test_fsm_foreign_session_row_not_adopted(db, db_bind):
    """Tenant/session guard: an id that points at ANOTHER session's row must
    never be filled — insert a fresh row instead (no cross-session merge)."""
    db.add(Message(id="msg-other-session", session_id="sess-OTHER",
                   role="assistant", content="", order=1))
    db.commit()
    result = _persist(db_bind, client_message_id="msg-other-session",
                      assistant_msg={"id": "msg-backend-3", "role": "assistant",
                                     "content": "the answer"})
    other = db.query(Message).filter(Message.id == "msg-other-session").first()
    assert (other.content or "") == "", "foreign row must not be written"
    filled = [r for r in _assistant_rows(db)
              if r.session_id == "sess-1" and (r.content or "").strip()]
    assert len(filled) == 1 and filled[0].content == "the answer"


def test_fsm_regenerate_keeps_client_as_writer(db, db_bind):
    """Existing regenerate contract: the client's row is the writer of record;
    the backend must not insert anything."""
    _persist(db_bind, regenerate=True, client_message_id="msg-client-1")
    rows = _assistant_rows(db)
    assert len(rows) == 1
    assert (rows[0].content or "") == ""  # untouched by the backend
    assert _assistant_rows(db)[0].id == "msg-client-1"


def test_fsm_commit_failure_rolls_back_atomically(db, db_bind, monkeypatch):
    def _boom(self):
        raise RuntimeError("disk full")

    monkeypatch.setattr("sqlalchemy.orm.Session.commit", _boom)
    with pytest.raises(RuntimeError):
        _persist(db_bind)
    # The fresh session rolled back on close: no partial writes. (The client
    # row still exists, untouched — its INSERT happened on the outer session.)
    rows = _assistant_rows(db)
    assert len(rows) == 1 and (rows[0].content or "") == ""


def test_fsm_done_payload_carries_identity():
    """The FSM done frame must attest persistence + echo the authoritative id
    so the client relinquishes its own content write (single writer)."""
    import inspect

    from app.routers import agents as ag

    src = inspect.getsource(ag)
    assert src.count('"answer_persisted"') >= 2  # react + fsm done frames
    assert '"assistant_message_id"' in src


# ── B/E. Restoration: history carries one authoritative row ────────────────
def test_history_restore_has_no_twin(db, db_bind):
    """Simulate: stream completes (client row filled) → a SECOND turn runs →
    history reload must show one assistant row per turn, no twins."""
    _persist(db_bind)
    # Second turn with its own client row.
    db.add(Message(id="msg-client-2", session_id="sess-1", role="assistant",
                   content="", order=2))
    db.commit()
    _persist(db_bind, client_message_id="msg-client-2",
             assistant_content="answer two",
             assistant_msg={"id": "msg-client-2", "role": "assistant",
                            "content": "answer two"},
             user_msg={"id": "msg-user-2", "role": "user", "content": "again"})
    rows = _assistant_rows(db)
    assert [r.id for r in rows] == ["msg-client-1", "msg-client-2"]
    assert [r.content for r in rows] == ["the answer", "answer two"]
