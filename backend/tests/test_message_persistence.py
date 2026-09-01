"""
Gap B: mid-turn checkpoint persistence.

The moment a report card or artifact is produced, the in-flight assistant
message is committed to the conversation. A dropped SSE stream or crash
afterwards must not erase finished work — reloading the conversation
shows the partial assistant message with its tool_calls + artifact_ids.

Locks in the checkpoint contract:
  1. The partial message lands in conv.messages and is committed.
  2. The in-flight ``messages`` list is NOT mutated (so the final
     assembly appends the authoritative assistant_msg with the same id
     and cleanly overwrites the checkpoint — never a duplicate).
  3. Repeated checkpoints keep exactly one message with that id.
"""

import sys, os
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import SessionLocal, engine, Base
from app.models.agent_conversation import AgentConversation
from app.routers.agents import _checkpoint_partial_assistant_msg


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def _make_conv(db) -> AgentConversation:
    conv = AgentConversation(
        id=str(uuid4()), title="ckpt", agent_name="tester", messages=[],
    )
    db.add(conv)
    db.commit()
    return conv


def test_checkpoint_persists_partial_with_tool_work(db):
    conv = _make_conv(db)
    mid = str(uuid4())
    in_flight = [{"id": "u1", "role": "user", "content": "make a pptx of sales"}]
    tcs = [{
        "id": "tc1", "name": "ask_data_agent", "status": "completed",
        "results": {"success": True, "artifact_id": "a1",
                    "report_card_payload": {"title": "Sales"}},
    }]

    _checkpoint_partial_assistant_msg(
        db, conv, in_flight, mid, tcs, ["a1"],
    )

    db.expire_all()
    fresh = db.query(AgentConversation).filter_by(id=conv.id).one()
    partial = [m for m in fresh.messages if m.get("id") == mid]
    assert len(partial) == 1
    assert partial[0]["artifact_ids"] == ["a1"]
    assert partial[0]["tool_calls"][0]["results"]["artifact_id"] == "a1"
    assert (
        partial[0]["tool_calls"][0]["results"]["report_card_payload"]["title"]
        == "Sales"
    )


def test_checkpoint_does_not_mutate_in_flight_messages(db):
    conv = _make_conv(db)
    mid = str(uuid4())
    in_flight = [{"id": "u1", "role": "user", "content": "hi"}]
    before = list(in_flight)

    _checkpoint_partial_assistant_msg(db, conv, in_flight, mid, [], [])

    assert in_flight == before  # untouched — final assembly stays authoritative

    # Final assembly pattern: append the authoritative message to the
    # in-flight list and rebind — the checkpoint id is replaced, not duped.
    in_flight.append({
        "id": mid, "role": "assistant", "content": "final answer",
    })
    conv.messages = list(in_flight)
    db.commit()
    db.expire_all()
    fresh = db.query(AgentConversation).filter_by(id=conv.id).one()
    same_id = [m for m in fresh.messages if m.get("id") == mid]
    assert len(same_id) == 1
    assert same_id[0]["content"] == "final answer"


def test_repeated_checkpoints_keep_single_message(db):
    conv = _make_conv(db)
    mid = str(uuid4())
    in_flight = []

    _checkpoint_partial_assistant_msg(db, conv, in_flight, mid, [], ["a1"])
    _checkpoint_partial_assistant_msg(db, conv, in_flight, mid, [], ["a1", "a2"])

    db.expire_all()
    fresh = db.query(AgentConversation).filter_by(id=conv.id).one()
    same_id = [m for m in fresh.messages if m.get("id") == mid]
    assert len(same_id) == 1
    assert same_id[0]["artifact_ids"] == ["a1", "a2"]


def test_checkpoint_empty_tool_work_still_persists(db):
    conv = _make_conv(db)
    mid = str(uuid4())
    _checkpoint_partial_assistant_msg(db, conv, [], mid, [], [])
    db.expire_all()
    fresh = db.query(AgentConversation).filter_by(id=conv.id).one()
    partial = [m for m in fresh.messages if m.get("id") == mid]
    assert len(partial) == 1
    assert "tool_calls" not in partial[0]
    assert "artifact_ids" not in partial[0]
