"""workflow_chat helper tests — the chat→workflow bridge.

Uses the same sqlite pattern as tests/routers/test_workflows_router.py:
in-memory engine, direct function calls, no TestClient.
"""

import sys
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import Base  # noqa: E402
import app.models  # noqa: F401,E402  register all models
from app.models.agent_app import AgentApp  # noqa: E402
from app.models.agent_conversation import AgentConversation  # noqa: E402
from app.models.workflow_run import WorkflowRun  # noqa: E402
from app.services import workflow_chat as wc  # noqa: E402
from app.services import workflow_runtime as wfr  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_engine)
_TestSession = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture
def db():
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)
    s = _TestSession()
    try:
        yield s
    finally:
        s.close()


def _user(uid="user-1"):
    return SimpleNamespace(id=uid)


def _workflow_agent(db, *, trigger="trigger_chat", aid=None, name="chat wf"):
    """trigger -> llm(prompt references {{inputs.<node>.history}}) -> output."""
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": trigger, "position": {"x": 0, "y": 0}, "data": {"label": "T", "config": {}}},
            {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0},
             "data": {"label": "L", "config": {"prompt": "History:\n{{inputs.n1.history}}\n\nReply to: {{inputs.n1.text}}"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "O", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
        ],
    }
    agent = AgentApp(
        id=aid or str(uuid.uuid4()), name=name, flow_mode=True, flow=flow,
        org_id="default-org", app_id="default-app",
    )
    db.add(agent)
    db.commit()
    return agent


def _conv(db, agent, messages):
    conv = AgentConversation(
        id=str(uuid.uuid4()), agent_name=agent.name, title=agent.name,
        messages=messages,
    )
    db.add(conv)
    db.commit()
    return conv


def test_has_chat_trigger():
    flow_chat = {"nodes": [{"type": "trigger_chat"}]}
    flow_manual = {"nodes": [{"type": "trigger_manual"}]}
    assert wc.has_chat_trigger(flow_chat) is True
    assert wc.has_chat_trigger(flow_manual) is False
    assert wc.has_chat_trigger({}) is False
    assert wc.has_chat_trigger(None) is False


def test_build_history_excludes_current_and_caps(db):
    messages = [
        {"role": "user", "content": f"turn {i}"} for i in range(15)
    ] + [{"role": "user", "content": "current message"}]
    hist = wc.build_history(messages, exclude_content="current message")
    assert len(hist) == 10
    assert hist[-1]["content"] == "turn 14"
    assert all(m["content"] != "current message" for m in hist)


def test_run_workflow_for_chat_completed_with_history(db, monkeypatch):
    captured = {}

    def fake_llm(prompt, model=None):
        captured["prompt"] = prompt
        return "workflow-answer"

    monkeypatch.setattr(wfr, "_sync_text_completion", fake_llm)
    agent = _workflow_agent(db)
    conv = _conv(db, agent, [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ])
    out = wc.run_workflow_for_chat(db, conv, agent, "current question", _user())
    assert out["status"] == "completed"
    assert out["content"] == "workflow-answer"
    assert out["run_id"]
    # History + current message reached the LLM prompt template.
    assert "User: earlier question" in captured["prompt"]
    assert "Assistant: earlier answer" in captured["prompt"]
    assert "Reply to: current question" in captured["prompt"]
    # A chat-triggered WorkflowRun was persisted.
    run = db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).first()
    assert run is not None and run.trigger == "chat"


def test_run_workflow_for_chat_no_chat_trigger(db):
    agent = _workflow_agent(db, trigger="trigger_manual")
    conv = _conv(db, agent, [])
    out = wc.run_workflow_for_chat(db, conv, agent, "hi", _user())
    assert out["status"] == "failed"
    assert "no Chat Trigger" in out["content"]
    assert out["run_id"] is None
    assert db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).count() == 0


def test_run_workflow_for_chat_failure_reports_error(db, monkeypatch):
    def boom(prompt, model=None):
        raise RuntimeError("llm provider down")

    monkeypatch.setattr(wfr, "_sync_text_completion", boom)
    agent = _workflow_agent(db)
    conv = _conv(db, agent, [])
    out = wc.run_workflow_for_chat(db, conv, agent, "hi", _user())
    assert out["status"] == "failed"
    assert "Workflow failed" in out["content"]
    assert "llm provider down" in out["content"]
    assert out["run_id"]
