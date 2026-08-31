"""Regression tests for Tool ``created_by_id`` claim semantics.

The Skills tab UI (frontend/src/pages/Toolkit.jsx) marks a tool as "added to
My Skills" by calling ``Tool.update(id, { created_by_id: <user.id> })`` on the
existing tool row. The generic entity API normally strips ``created_by_id``
because for ChatSession/Project/KnowledgeBase it is the user-isolation stamp
that the row-level scoping filter relies on (see ``_IMMUTABLE_FIELDS`` in
``entity_service``).

On ``Tool`` it is NOT a security boundary — ``Tool`` is a global catalog, not
in ``USER_SCOPED_ENTITIES``. The frontend renders the My Skills modal with
``tools.filter(x => x.created_by_id === ownerId)`` so the claim MUST be
persistable, otherwise the click is a silent no-op and the user gets a
"No skills yet" modal after reload — that was the 2026-08-28 bug.

These tests pin the rule: claim is allowed on Tool, never on any other
model, and only with the requesting user's own id.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.tool import Tool
from app.models.user import User
from app.services.entity_service import (
    _CLAIMABLE_FIELDS,
    _CLAIMABLE_MODELS,
    _IMMUTABLE_FIELDS,
    _apply_claim_updates,
    _filter_data,
    update_record,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_claim_models_only_lists_tool():
    # Pin the carve-out: only Tool is allowed to claim created_by_id.
    # Adding anything else here is a security review and must be intentional.
    assert _CLAIMABLE_MODELS == {"Tool"}
    assert _CLAIMABLE_FIELDS == {"created_by_id"}


def test_filter_data_still_strips_created_by_id():
    # The general API must continue to strip created_by_id so other entities
    # stay protected — _apply_claim_updates re-applies it for Tool only.
    assert "created_by_id" in _IMMUTABLE_FIELDS
    out = _filter_data(Tool, {"name": "x", "created_by_id": "someone-else", "description": "d"})
    assert "created_by_id" not in out
    assert out["name"] == "x"
    assert out["description"] == "d"


def test_apply_claim_rejects_cross_user_reassignment():
    # User A cannot claim for user B — guards against IDOR even within the
    # Tool claim carve-out.
    db = _session()
    record = Tool(name="frontend-slides", created_by_id=None)
    db.add(record)
    db.commit()

    _apply_claim_updates(record, Tool, {"created_by_id": "user-b"}, claim_user_id="user-a")
    db.commit()
    assert record.created_by_id is None, "cross-user claim must be silently ignored"


def test_apply_claim_rejects_when_no_claim_user_id():
    # No claim_user_id means unauthenticated — the helper must be a no-op.
    db = _session()
    record = Tool(name="frontend-slides", created_by_id=None)
    db.add(record)
    db.commit()

    _apply_claim_updates(record, Tool, {"created_by_id": None}, claim_user_id=None)
    db.commit()
    assert record.created_by_id is None


def test_apply_claim_succeeds_on_unowned_tool():
    db = _session()
    record = Tool(name="frontend-slides", created_by_id=None)
    db.add(record)
    db.commit()

    _apply_claim_updates(record, Tool, {"created_by_id": "user-a"}, claim_user_id="user-a")
    db.commit()
    assert record.created_by_id == "user-a"


def test_apply_claim_allows_reclaim():
    # User B can re-claim a Tool user A previously claimed.  Tool is a
    # global catalog, not user-isolated, so whoever clicks last wins.
    db = _session()
    record = Tool(name="frontend-slides", created_by_id="user-a")
    db.add(record)
    db.commit()

    _apply_claim_updates(record, Tool, {"created_by_id": "user-b"}, claim_user_id="user-b")
    db.commit()
    assert record.created_by_id == "user-b"


def test_apply_claim_ignores_non_claim_models():
    # Use ChatSession-shaped model class name to prove the helper is
    # a no-op for non-Tool models.
    from app.models.chat_session import ChatSession

    db = _session()
    record = ChatSession(title="x", created_by_id=None)
    db.add(record)
    db.commit()

    _apply_claim_updates(record, ChatSession, {"created_by_id": "user-a"}, claim_user_id="user-a")
    db.commit()
    assert record.created_by_id is None, "ChatSession must stay immutable — no claim"


def test_update_record_end_to_end_claim_persists():
    # End-to-end: routing through update_record() — which is what the
    # router calls — must persist the claim.  This is the test that
    # would have failed before the fix.
    db = _session()
    record = Tool(name="dashboard-generation", description="d", source="builtin",
                  created_by_id=None)
    db.add(record)
    db.commit()
    rid = record.id

    # Tool is NOT in USER_SCOPED_ENTITIES so owner_id=None in production,
    # but claim_user_id carries the caller's id.
    result = update_record(Tool, rid, {"created_by_id": "user-a"}, db,
                           owner_id=None, claim_user_id="user-a")
    assert result is not None
    assert result["created_by_id"] == "user-a"


def test_update_record_rejects_cross_user_claim():
    db = _session()
    record = Tool(name="dashboard-generation", source="builtin", created_by_id=None)
    db.add(record)
    db.commit()
    rid = record.id

    # Client sends a claim for someone else — the row stays unclaimed.
    result = update_record(Tool, rid, {"created_by_id": "user-b"}, db,
                           owner_id=None, claim_user_id="user-a")
    assert result["created_by_id"] is None


def test_update_record_other_models_still_reject_created_by_id():
    # The carve-out must NOT leak — ChatSession must still strip
    # created_by_id (it's a security boundary there).
    from app.models.chat_session import ChatSession

    db = _session()
    cs = ChatSession(title="x", created_by_id="user-a")
    db.add(cs)
    db.commit()
    cid = cs.id

    # Even with claim_user_id passed, ChatSession stays immutable.
    result = update_record(ChatSession, cid, {"title": "y", "created_by_id": "user-b"}, db,
                           owner_id="user-a", claim_user_id="user-a")
    assert result["title"] == "y"
    assert result["created_by_id"] == "user-a", "ChatSession must not allow created_by_id writes"