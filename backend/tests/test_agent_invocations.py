"""Tests for the agent_invocations recorder (Phase 1 agent-gaps build)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import uuid

import pytest

from app.database import Base, engine, SessionLocal
from app.models.agent_app import AgentApp
from app.models.agent_invocation import AgentInvocation
from app.services.agent_invocations import record_invocation
import app.models  # noqa: F401


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    s.query(AgentInvocation).delete()
    s.query(AgentApp).delete()
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _seed_app(db):
    app = AgentApp(id=str(uuid.uuid4()), name="general_assistant")
    db.add(app)
    db.commit()
    return app.id


def test_record_conversation_invocation(db):
    app_id = _seed_app(db)
    row_id = record_invocation(
        db,
        agent_app_id=app_id,
        conversation_id="conv-1",
        user_id="user-1",
        invocation_type="conversation",
        input_message="Build a dashboard",
        status="completed",
        duration_ms=1234,
        token_usage={"prompt_tokens": 100, "completion_tokens": 50},
        cost_amount=0.0042,
        confidence_score=0.8,
        trace_id="trace-abc",
    )
    assert row_id is not None
    row = db.query(AgentInvocation).filter_by(id=row_id).first()
    assert row is not None
    assert row.agent_app_id == app_id
    assert row.conversation_id == "conv-1"
    assert row.invocation_type == "conversation"
    assert row.duration_ms == 1234
    assert row.token_usage["prompt_tokens"] == 100
    assert row.cost_amount == 0.0042
    assert row.trace_id == "trace-abc"


def test_record_sub_agent_with_parent_link(db):
    app_id = _seed_app(db)
    parent = record_invocation(
        db, agent_app_id=app_id, invocation_type="conversation", status="running"
    )
    child = record_invocation(
        db,
        agent_app_id=app_id,
        conversation_id="conv-1",
        invocation_type="sub_agent",
        input_message="analyze customer revenue",
        status="completed",
        duration_ms=500,
        trace_id="trace-abc",
        parent_invocation_id=parent,
    )
    assert child is not None
    row = db.query(AgentInvocation).filter_by(id=child).first()
    assert row.invocation_type == "sub_agent"
    assert row.span_id == parent  # parent linkage via span_id


def test_record_resolves_app_by_name(db):
    _seed_app(db)  # name = general_assistant
    row_id = record_invocation(db, invocation_type="conversation")
    assert row_id is not None
    row = db.query(AgentInvocation).filter_by(id=row_id).first()
    assert row.agent_app_id is not None


def test_record_missing_app_is_skipped(db):
    row_id = record_invocation(db, invocation_type="conversation")
    assert row_id is None  # no app rows exist in this db


def test_record_failure_never_raises(db):
    # A closed session must not propagate — best-effort contract.
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import create_engine

    eng = create_engine("sqlite:///:memory:")
    sess = sessionmaker(bind=eng)()
    sess.close()
    assert record_invocation(sess, invocation_type="conversation") is None
