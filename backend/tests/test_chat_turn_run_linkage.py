"""Phase 8.1 Stage 1 — AgentRun-to-message linkage for the v3 chat stream.

``record_chat_turn_run`` gives every v3 chat turn a real AgentRun (the
provenance choke point) WITHOUT changing stream behavior: the run is
created up-front, tool-call steps are appended from the turn's executed
tool calls (blocked/denied calls are NOT recorded as executions), and the
run is finalized with the honest terminal status. Fail-open: a recording
failure never breaks the chat stream. Returns the run_id (or None).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_run import AgentRun
from app.models.agent_run_step import AgentRunStep
from app.services.harness.turn_recorder import record_chat_turn_run

ORG = "org-acme"

TOOL_CALLS = [
    {"id": "t1", "name": "ask_data_agent", "status": "completed",
     "results": {"rows": 5}},
    {"id": "t2", "name": "delegate_task", "status": "completed", "results": {}},
    {"id": "t3", "name": "delete_record", "status": "blocked",
     "results": {"denied": True}},
    {"id": "t4", "name": "web_search", "status": "error", "results": {}},
]


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        AgentRun.__table__, AgentRunStep.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def test_creates_run_with_executed_tool_steps(db):
    run_id = record_chat_turn_run(
        db, org_id=ORG, user_id="u1", conversation_id="c1",
        agent_name="general_assistant", task="check orders",
        tool_calls=TOOL_CALLS, status="completed")
    assert run_id
    run = db.query(AgentRun).filter_by(run_id=run_id).one()
    assert run.status == "completed"
    assert run.org_id == ORG
    ctx = __import__("json").loads(run.caller_context)
    assert ctx["org_id"] == ORG and ctx["conversation_id"] == "c1"
    # executed + failed steps recorded; blocked DENIAL is not an execution
    steps = db.query(AgentRunStep).filter_by(run_id=run_id) \
        .order_by(AgentRunStep.step_index).all()
    names = [st.tool_name for st in steps]
    assert names == ["ask_data_agent", "delegate_task", "web_search"]
    statuses = [st.status for st in steps]
    assert statuses == ["ok", "ok", "error"]


def test_failed_turn_status_is_verbatim(db):
    run_id = record_chat_turn_run(
        db, org_id=ORG, user_id="u1", conversation_id="c1",
        agent_name="a", task="t", tool_calls=[], status="failed")
    run = db.query(AgentRun).filter_by(run_id=run_id).one()
    assert run.status == "failed"


def test_no_tools_still_creates_run(db):
    run_id = record_chat_turn_run(
        db, org_id=ORG, user_id=None, conversation_id="c1",
        agent_name="a", task="t", tool_calls=None, status="completed")
    run = db.query(AgentRun).filter_by(run_id=run_id).one()
    assert run.tool_call_count == 0


def test_recording_failure_never_raises(db, monkeypatch):
    from app.services.harness import run_service
    monkeypatch.setattr(
        run_service.AgentRunService, "create_run_record",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert record_chat_turn_run(
        db, org_id=ORG, user_id="u1", conversation_id="c1",
        agent_name="a", task="t", tool_calls=TOOL_CALLS,
        status="completed") is None
