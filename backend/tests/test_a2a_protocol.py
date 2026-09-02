"""Tests for the A2A protocol layer (Phase 1: external agent interop).

Covers AgentCard building, the task store lifecycle (with a fake executor),
the JSON-RPC router surface, and the remote client (mocked HTTP).
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
from app.models.agent_app import AgentApp
from app.models.a2a_task import A2aTask
import app.models  # noqa: F401  register all models


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _seed_agent(db, name="sales-analyst"):
    agent = AgentApp(
        name=name,
        description="Sales analyst agent for A2A tests",
        prompt_identity=f"You are {name}.",
        status="published",
        org_id="default-org",
        app_id="default-app",
    )
    db.add(agent)
    db.commit()
    return agent


# ── AgentCard ───────────────────────────────────────────────────────────

def test_agent_card_shape():
    from app.services.a2a.agent_card import build_agent_card

    agent = {
        "id": "a1",
        "name": "sales-analyst",
        "description": "Analyzes sales data",
        "capabilities": ["Database Analysis", "Make PPT"],
    }
    card = build_agent_card(agent, base_url="https://zhanlu.example.com")
    assert card["name"] == "sales-analyst"
    assert card["description"] == "Analyzes sales data"
    assert card["version"] == "1.0.0"
    assert card["authentication"]["schemes"] == ["bearer"]
    assert card["defaultInputModes"] == ["text"]
    assert card["defaultOutputModes"] == ["text"]
    assert any(s["name"] == "Database Analysis" for s in card["skills"])


# ── Task store + executor ───────────────────────────────────────────────

class FakeExecutor:
    """Deterministic executor: completes the task with a canned reply."""

    def __init__(self):
        self.calls = []

    async def run(self, *, task, agent_app, db):
        self.calls.append(task.id)
        from app.services.a2a import tasks as tasks_mod
        tasks_mod.publish(task.id, {"type": "status", "status": "working"})
        store = tasks_mod.A2aTaskStore(db)
        store.update_status(task, "completed", error=None)
        store.append_artifact(task, {"name": "reply", "mimeType": "text/plain", "text": "done: hello"})


def test_task_store_lifecycle(db, monkeypatch):
    from app.services.a2a import tasks as tasks_mod

    agent = _seed_agent(db)
    store = tasks_mod.A2aTaskStore(db)
    task = store.create(
        agent_app_id=agent.id,
        message_parts=[{"text": "What are March sales?"}],
        metadata=None,
        external=False,
        created_by_id=None,
    )
    assert task.status == "submitted"

    fake = FakeExecutor()
    monkeypatch.setattr(tasks_mod, "get_executor", lambda name="default": fake)
    db.add(agent)
    import asyncio
    asyncio.run(tasks_mod.dispatch_task(db, task.id))

    db.refresh(task)
    assert task.status == "completed"
    result = store.get(task.id).result_json
    payload = json.loads(result)
    assert payload["artifacts"][0]["name"] == "reply"

    store.cancel(task)
    db.refresh(task)
    assert task.status == "canceled"


def test_task_cancel_before_dispatch(db):
    from app.services.a2a import tasks as tasks_mod

    agent = _seed_agent(db)
    store = tasks_mod.A2aTaskStore(db)
    task = store.create(agent_app_id=agent.id, message_parts=[{"text": "hi"}],
                        metadata=None, external=False, created_by_id=None)
    store.cancel(task)
    assert task.status == "canceled"


# ── Router (JSON-RPC surface) ───────────────────────────────────────────

@pytest.fixture
def client(db, monkeypatch):
    from app.routers import a2a as a2a_router

    app = FastAPI()
    app.include_router(a2a_router.router)

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[a2a_router.get_a2a_auth] = lambda: {"user_id": "u1"}

    fake = FakeExecutor()
    monkeypatch.setattr(a2a_router.tasks_mod, "get_executor", lambda name="default": fake)
    return TestClient(app)


def test_router_jsonrpc_send_and_get(client, db):
    agent = _seed_agent(db)
    resp = client.post("/a2a", json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tasks/send",
        "params": {
            "id": f"task-{uuid.uuid4().hex[:8]}",
            "agent_id": agent.id,
            "message": {"role": "user", "parts": [{"text": "Hello from external agent"}]},
        },
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("jsonrpc") == "2.0"
    task = body["result"]
    assert task["status"] in {"submitted", "working", "completed"}

    # Fake executor completes synchronously during dispatch (awaited inline).
    get_resp = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 2, "method": "tasks/get",
        "params": {"id": task["id"]},
    })
    assert get_resp.status_code == 200
    fetched = get_resp.json()["result"]
    assert fetched["status"] == "completed"
    assert "hello" in json.dumps(fetched.get("artifacts", []))


def test_router_agent_card(client, db):
    _seed_agent(db, name="forecast-agent")
    resp = client.get("/a2a/agent-card")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"]
    assert card["skills"]


# ── Remote client ───────────────────────────────────────────────────────

def test_remote_client_send_and_get(monkeypatch):
    import httpx

    from app.services.a2a import client as client_mod

    sent = {"id": "t1", "status": "submitted"}
    done = {"id": "t1", "status": "completed", "artifacts": [{"name": "r", "mimeType": "text/plain", "text": "ok"}]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            payload = kwargs.get("json") or {}
            method = payload.get("method")
            if method == "tasks/get":
                return httpx.Response(200, request=httpx.Request("POST", url), json={"jsonrpc": "2.0", "id": payload.get("id"), "result": done})
            return httpx.Response(200, request=httpx.Request("POST", url), json={"jsonrpc": "2.0", "id": payload.get("id"), "result": sent})

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", FakeAsyncClient)

    import asyncio
    result = asyncio.run(client_mod.send_task(
        endpoint="https://ext.example.com/",
        message_parts=[{"text": "run analysis"}],
        agent_id="a1",
        token="tok",
    ))
    assert result["status"] == "submitted"

    got = asyncio.run(client_mod.get_task("https://ext.example.com/", "t1", token="tok"))
    assert got["status"] == "completed"
