"""Router-level integration: the v2 chat message endpoint runs flow-mode
workflows via the workflow runtime (trigger="chat") and returns the final
output as the assistant message. Same sqlite pattern as
test_workflow_chat.py, but drives the actual ``agents.add_message`` route
function so the branch insertion point is exercised end-to-end.
"""

import asyncio
import os
import sys
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.database import Base  # noqa: E402
import app.models  # noqa: F401,E402  register all models
from app.models.agent_app import AgentApp  # noqa: E402
from app.models.agent_conversation import AgentConversation  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.workflow_run import WorkflowRun  # noqa: E402
from app.routers import agents  # noqa: E402
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


def _user(db):
    u = User(id="user-1", email="u@zhanlu.dev", password_hash="x", role="admin", full_name="Tester")
    db.add(u)
    db.commit()
    return u


def _workflow_agent(db, *, trigger="trigger_chat", name="chat wf"):
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
        id=str(uuid.uuid4()), name=name, flow_mode=True, flow=flow,
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


def test_v2_add_message_runs_flow_mode_workflow(db, monkeypatch):
    captured = {}

    def fake_llm(prompt, model=None):
        captured["prompt"] = prompt
        return "wf-answer"

    monkeypatch.setattr(wfr, "_sync_text_completion", fake_llm)
    user = _user(db)
    agent = _workflow_agent(db)
    conv = _conv(db, agent, [
        {"role": "user", "content": "prior q"},
        {"role": "assistant", "content": "prior a"},
    ])

    result = asyncio.run(agents.add_message(
        app_id="default-app",
        conversation_id=conv.id,
        body={"content": "current q"},
        db=db,
        user=user,
    ))

    msgs = result["messages"]
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["content"] == "wf-answer"
    # Prior turns + current user turn persisted; the workflow's LLM prompt
    # received prior turns only (current message excluded from history).
    assert msgs[-2]["role"] == "user" and msgs[-2]["content"] == "current q"
    assert "User: prior q" in captured["prompt"]
    assert "Assistant: prior a" in captured["prompt"]
    assert "Reply to: current q" in captured["prompt"]
    assert "User: current q" not in captured["prompt"]
    # A chat-triggered WorkflowRun was persisted with per-node steps.
    run = db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).first()
    assert run is not None and run.trigger == "chat"


def test_v2_add_message_flow_without_chat_trigger_reports_error(db):
    user = _user(db)
    agent = _workflow_agent(db, trigger="trigger_manual")
    conv = _conv(db, agent, [])

    result = asyncio.run(agents.add_message(
        app_id="default-app",
        conversation_id=conv.id,
        body={"content": "hi"},
        db=db,
        user=user,
    ))

    msgs = result["messages"]
    assert msgs[-1]["role"] == "assistant"
    assert "no Chat Trigger" in msgs[-1]["content"]
    assert db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).count() == 0


def test_v3_stream_runs_flow_mode_workflow(db, monkeypatch):
    captured = {}

    def fake_llm(prompt, model=None):
        captured["prompt"] = prompt
        return "wf-answer"

    monkeypatch.setattr(wfr, "_sync_text_completion", fake_llm)
    user = _user(db)
    agent = _workflow_agent(db)
    conv = _conv(db, agent, [
        {"role": "user", "content": "prior q"},
        {"role": "assistant", "content": "prior a"},
    ])

    resp = asyncio.run(agents.add_message_stream(
        app_id="default-app",
        conversation_id=conv.id,
        body={"content": "current q"},
        db=db,
        user=user,
    ))
    assert resp.media_type == "text/event-stream"

    async def drain():
        chunks = []
        async for c in resp.body_iterator:
            chunks.append(c)
        return "".join(chunks) if chunks and isinstance(chunks[0], str) else b"".join(chunks).decode()

    text = asyncio.run(drain())
    assert '"type": "done"' in text
    assert "wf-answer" in text
    # History reached the workflow LLM prompt; current turn excluded.
    assert "User: prior q" in captured["prompt"]
    assert "Reply to: current q" in captured["prompt"]
    assert "User: current q" not in captured["prompt"]
    # Assistant message persisted + chat-triggered run recorded.
    refreshed = db.query(AgentConversation).filter(AgentConversation.id == conv.id).first()
    assert refreshed.messages[-1]["role"] == "assistant"
    assert refreshed.messages[-1]["content"] == "wf-answer"
    run = db.query(WorkflowRun).filter(WorkflowRun.agent_app_id == agent.id).first()
    assert run is not None and run.trigger == "chat"
