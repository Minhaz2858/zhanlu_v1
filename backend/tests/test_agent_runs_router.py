"""Tests for the new ``/api/agent-runs/...`` router (P0-3).

The plan requires three endpoints exposing the AgentRun / AgentRunStep
audit trail:

  * ``GET  /api/agent-runs/{run_id}``            — status + steps
  * ``POST /api/agent-runs/{run_id}/resume``     — replay from last checkpoint
  * ``GET  /api/agent-runs/conversations/{cid}``  — list runs for a conversation

These tests directly invoke the router functions (skipping FastAPI's
TestClient) to keep the test fully isolated from the rest of the app —
no need to start up Postgres / Redis / hook loaders etc.  We patch the
``get_db`` dependency on the actual router module so the route functions
themselves use our test session.
"""
import os
import sys
import uuid
import json
from datetime import datetime, timezone

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # force all model classes to register with Base
from app.database import Base
from app.models.agent_run import AgentRun
from app.models.agent_run_step import AgentRunStep
from app.routers import agent_runs as agent_runs_module
from app.routers.agent_runs import get_run, resume_run, list_runs_for_conversation
from app.models.user import User


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    AgentRun.__table__.create(engine, checkfirst=True)
    AgentRunStep.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_run(db, run_id=None, agent_name="test_agent", task="hi",
              status="running", conversation_id=None, parent_run_id=None):
    rid = run_id or uuid.uuid4().hex[:32]
    r = AgentRun(
        run_id=rid,
        agent_name=agent_name,
        task=task,
        status=status,
        mode="inline",
        iterations=0,
        tool_call_count=0,
        caller_context=json.dumps({"conversation_id": conversation_id}) if conversation_id else None,
        parent_run_id=parent_run_id,
    )
    db.add(r)
    db.commit()
    return rid


def _seed_step(db, run_id, step_type="llm_call", step_index=0,
               tool_name=None, iteration=0, duration_ms=10):
    sid = uuid.uuid4().hex[:32]
    s = AgentRunStep(
        step_id=sid,
        run_id=run_id,
        step_type=step_type,
        step_index=step_index,
        tool_name=tool_name,
        iteration=iteration,
        duration_ms=duration_ms,
    )
    db.add(s)
    db.commit()
    return sid


# ── Endpoint 1: GET /api/agent-runs/{run_id} ───────────────────────────────

def test_get_run_returns_status_and_steps(db_session):
    rid = _seed_run(db_session, status="running", conversation_id="conv-1")
    _seed_step(db_session, rid, step_type="llm_call", step_index=0)
    _seed_step(db_session, rid, step_type="tool_call", step_index=1, tool_name="foo")
    out = get_run(run_id=rid, db=db_session, user=None)
    assert out.run_id == rid
    assert out.status == "running"
    assert out.agent_name == "test_agent"
    assert len(out.steps) == 2
    assert out.steps[0].step_index == 0
    assert out.steps[1].step_index == 1
    assert out.steps[1].tool_name == "foo"


def test_get_run_raises_404_for_unknown_id(db_session):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        get_run(run_id="a" * 32, db=db_session, user=None)
    assert exc_info.value.status_code == 404


# ── Endpoint 2: POST /api/agent-runs/{run_id}/resume ────────────────────────

def test_resume_run_creates_child_and_marks_parent_failed(db_session, monkeypatch):
    rid = _seed_run(db_session, status="failed")

    # Monkey-patch create_run_record on the service instance used by the
    # router so we can control the result and assert it's called.
    from app.services.harness.run_service import AgentRunService
    calls = []
    def _fake_create(self, **kwargs):
        calls.append(kwargs)
        new_id = uuid.uuid4().hex[:32]
        r = AgentRun(
            run_id=new_id,
            agent_name=kwargs["agent_name"],
            task=kwargs["task"],
            mode=kwargs.get("mode", "inline"),
            status="queued",
            parent_run_id=kwargs.get("parent_run_id"),
            caller_context=kwargs.get("caller_context"),
            iterations=0,
            tool_call_count=0,
        )
        db_session.add(r)
        db_session.commit()
        return r
    monkeypatch.setattr(AgentRunService, "create_run_record", _fake_create)
    monkeypatch.setattr(AgentRunService, "finalize_run",
                        lambda self, run_id, status, db=None: True)

    out = resume_run(run_id=rid, db=db_session, user=None)
    assert out.parent_run_id == rid
    assert out.status == "queued"
    assert len(calls) == 1
    assert calls[0]["parent_run_id"] == rid

    # Parent must now be marked failed.
    parent = db_session.query(AgentRun).filter_by(run_id=rid).one()
    assert parent.status == "failed"


def test_resume_run_raises_404_for_unknown_id(db_session):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        resume_run(run_id="a" * 32, db=db_session, user=None)
    assert exc_info.value.status_code == 404


def test_resume_run_rejects_completed_run(db_session):
    rid = _seed_run(db_session, status="completed")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        resume_run(run_id=rid, db=db_session, user=None)
    assert exc_info.value.status_code == 409


# ── Endpoint 3: GET /api/agent-runs/conversations/{cid} ───────────────────

def test_list_runs_for_conversation_filters_correctly(db_session):
    rid_a = _seed_run(db_session, conversation_id="conv-1", status="completed")
    rid_b = _seed_run(db_session, conversation_id="conv-1", status="failed")
    _seed_run(db_session, conversation_id="conv-2", status="completed")
    out = list_runs_for_conversation(conversation_id="conv-1", db=db_session, user=None)
    ids = {r.run_id for r in out}
    assert ids == {rid_a, rid_b}


def test_list_runs_for_conversation_returns_empty_for_unknown(db_session):
    out = list_runs_for_conversation(conversation_id="never-seen", db=db_session, user=None)
    assert out == []
