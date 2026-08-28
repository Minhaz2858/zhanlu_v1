"""Tests for the dashboard-delete conversation cascade helper.

``delete_bound_conversations(db, slug, id)`` removes AgentConversation rows
whose metadata_ binds them to a dashboard app (mode == "dashboard" and the
slug or id match). Matching is done in Python (not SQL) so the same helper
works on SQLite test DBs and Postgres production.

Uses the app's shared in-memory SessionLocal (tests/conftest.py points
DATABASE_URL at a shared sqlite and swaps SessionLocal onto a StaticPool
engine), so plain ``SessionLocal()`` works here.
"""

import uuid

import pytest

from app.database import Base, SessionLocal, engine
from app.models.agent_conversation import AgentConversation
from app.services.dashboard_app.cascade import delete_bound_conversations


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db_session():
    s = SessionLocal()
    try:
        # Clean slate per test so rows from other tests never leak through.
        s.query(AgentConversation).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _conv(db, *, slug=None, dash_id=None, mode="dashboard"):
    c = AgentConversation(
        id=str(uuid.uuid4()),
        agent_name="dashboard_builder",
        title="bound chat",
        status="active",
    )
    if mode is None:
        c.metadata_ = None
    else:
        c.metadata_ = {"mode": mode}
        if slug:
            c.metadata_["dashboard_slug"] = slug
        if dash_id:
            c.metadata_["dashboard_id"] = dash_id
    db.add(c)
    db.commit()
    return c


def test_deletes_matching_slug_leaves_others(db_session):
    bound = _conv(db_session, slug="sales-overview")
    other = _conv(db_session, slug="other-dashboard")
    id_only = _conv(db_session, dash_id="unrelated-id")

    count = delete_bound_conversations(db_session, "sales-overview", "never-matching-id")

    assert count == 1
    remaining = db_session.query(AgentConversation).all()
    assert {c.id for c in remaining} == {other.id, id_only.id}
    assert bound.id not in {c.id for c in remaining}


def test_deletes_matching_id(db_session):
    by_id = _conv(db_session, dash_id="dash-123")
    other = _conv(db_session, slug="other-slug", dash_id="dash-999")

    count = delete_bound_conversations(db_session, "sales-overview", "dash-123")

    assert count == 1
    remaining = db_session.query(AgentConversation).all()
    assert [c.id for c in remaining] == [other.id]


def test_none_metadata_returns_zero(db_session):
    _conv(db_session, mode=None)  # metadata_ is NULL — must not crash
    _conv(db_session, mode="chat")  # not a dashboard-bound conversation

    count = delete_bound_conversations(db_session, "sales-overview", "dash-123")

    assert count == 0
    assert db_session.query(AgentConversation).count() == 2


def test_non_dict_metadata_skipped_and_others_deleted(db_session):
    # A malformed metadata_ row (JSON list/string instead of a dict) must be
    # skipped, NOT crash the whole cascade (previously the AttributeError
    # propagated and aborted every deletion → orphaned conversations).
    bad_list = AgentConversation(
        id=str(uuid.uuid4()),
        agent_name="dashboard_builder",
        title="malformed list metadata",
        status="active",
        metadata_=["mode", "dashboard", "dashboard_slug", "sales-overview"],
    )
    bad_str = AgentConversation(
        id=str(uuid.uuid4()),
        agent_name="dashboard_builder",
        title="malformed string metadata",
        status="active",
        metadata_="mode=dashboard dashboard_slug=sales-overview",
    )
    db_session.add_all([bad_list, bad_str])
    bound = _conv(db_session, slug="sales-overview")
    other = _conv(db_session, slug="other-dashboard")

    count = delete_bound_conversations(db_session, "sales-overview", "never-matching-id")

    assert count == 1
    remaining = db_session.query(AgentConversation).all()
    remaining_ids = {c.id for c in remaining}
    assert bound.id not in remaining_ids
    assert bad_list.id in remaining_ids
    assert bad_str.id in remaining_ids
    assert other.id in remaining_ids


def test_empty_db_returns_zero(db_session):
    assert db_session.query(AgentConversation).count() == 0
    assert delete_bound_conversations(db_session, "sales-overview", "dash-123") == 0
