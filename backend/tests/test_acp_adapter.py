"""Tests for the ACP REST adapter (#3) and A2A SSE streaming client (#4)."""
import asyncio
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.database import Base, engine, SessionLocal, get_db
import app.models  # noqa: F401  register all models
from app.models.agent_app import AgentApp
from app.services.a2a import client as a2a_client
from app.services.a2a import tasks as tasks_mod


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


class _FakeExecutor:
    async def run(self, *, task, agent_app, db):
        store = tasks_mod.A2aTaskStore(db)
        store.update_status(task, "completed")
        store.append_artifact(
            task, {"name": "reply", "mimeType": "text/plain",
                   "parts": [{"type": "text", "text": "acp answer"}]})


@pytest.fixture
def client(db, monkeypatch):
    from app.routers import acp as acp_router

    app = FastAPI()
    app.include_router(acp_router.router)

    def _override_db():
        yield db

    class _U:
        id = "u1"

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[acp_router.get_current_user_optional] = lambda: _U()
    monkeypatch.setattr(acp_router.tasks_mod, "get_executor",
                        lambda name="default": _FakeExecutor())
    return TestClient(app)


@pytest.fixture
def published(db):
    agent = AgentApp(
        name="acp-agent", description="ACP test agent", prompt_identity="x",
        status="published", org_id="default-org", app_id="default-app",
    )
    db.add(agent)
    db.commit()
    return agent


# ── #3 ACP adapter ──────────────────────────────────────────────────────

def test_acp_agents_discovery(client, published):
    resp = client.get("/acp/agents")
    assert resp.status_code == 200
    agents = resp.json()
    match = [a for a in agents if a["name"] == "acp-agent"]
    assert match and match[0]["metadata"]["protocol"] == "acp"
    assert "runs_url" in match[0]["metadata"]


def test_acp_run_lifecycle_wait(client, published):
    run = client.post(
        f"/acp/agents/{published.id}/runs?wait=true",
        json={"message": {"parts": [{"type": "text", "text": "hello"}]}},
    )
    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "completed"
    assert body["protocol"] == "acp"

    state = client.get(f"/acp/runs/{body['id']}")
    assert state.json()["status"] == "completed"

    msgs = client.get(f"/acp/runs/{body['id']}/messages")
    parts = msgs.json()["parts"]
    assert any(p.get("text") == "acp answer" for p in parts)


def test_acp_run_requires_parts(client, published):
    r = client.post(f"/acp/agents/{published.id}/runs", json={})
    assert r.status_code == 422


def test_acp_unknown_agent_404(client, db):
    r = client.post("/acp/agents/nope/runs", json={
        "message": {"parts": [{"type": "text", "text": "x"}]}})
    assert r.status_code == 404


def test_acp_cancel(client, published, monkeypatch):
    # Keep the run unsettled so cancel has something to cancel.
    from app.routers import acp as acp_router

    class _StuckExecutor:
        async def run(self, **kw):
            await asyncio.sleep(30)

    monkeypatch.setattr(tasks_mod, "get_executor",
                        lambda name="default": _StuckExecutor())
    run = client.post(
        f"/acp/agents/{published.id}/runs?wait=false",
        json={"message": {"parts": [{"type": "text", "text": "long"}]}},
    )
    run_id = run.json()["id"]
    cancel = client.post(f"/acp/runs/{run_id}/cancel")
    assert cancel.json()["status"] == "canceled"
    monkeypatch.setattr(tasks_mod, "get_executor",
                        lambda name="default": _FakeExecutor())


def test_acp_unauthenticated_401(client, published, monkeypatch):
    from app.routers import acp as acp_router

    class _Anon:
        id = "u1"

    # remove the user override → dependency resolves to None → 401
    client.app.dependency_overrides.pop(acp_router.get_current_user_optional, None)
    try:
        r = client.post(f"/acp/agents/{published.id}/runs", json={
            "message": {"parts": [{"type": "text", "text": "x"}]}})
        assert r.status_code == 401
    finally:
        client.app.dependency_overrides[acp_router.get_current_user_optional] = (
            lambda: _Anon())


# ── #4 A2A SSE streaming client ─────────────────────────────────────────

def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)


@pytest.mark.asyncio
async def test_streaming_collects_intermediate_and_final(monkeypatch):
    frames = [
        {"jsonrpc": "2.0", "id": "1", "result": {"id": "t1",
         "status": {"state": "working"}}},
        {"jsonrpc": "2.0", "id": "1", "result": {"id": "t1",
         "status": {"state": "completed"},
         "artifacts": [{"parts": [{"text": "done"}]}]}},
    ]
    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"].startswith("text/event-stream")
        return httpx.Response(200, content=body,
                              headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(a2a_client, "_new_client", lambda t: _mock_client(handler))
    events = []
    final = await a2a_client.send_task_streaming(
        "https://x.example.com/a2a",
        [{"type": "text", "text": "hi"}], "agent-1", on_event=events.append,
    )
    assert final["status"]["state"] == "completed"
    assert final["artifacts"][0]["parts"][0]["text"] == "done"
    assert [e["status"]["state"] for e in events] == ["working", "completed"]


@pytest.mark.asyncio
async def test_streaming_falls_back_to_plain_json(monkeypatch):
    result = {"id": "t2", "status": {"state": "completed"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": "1", "result": result},
            headers={"content-type": "application/json"})

    monkeypatch.setattr(a2a_client, "_new_client", lambda t: _mock_client(handler))
    final = await a2a_client.send_task_streaming(
        "https://x.example.com/a2a",
        [{"type": "text", "text": "hi"}], "agent-1",
    )
    assert final == result


@pytest.mark.asyncio
async def test_streaming_sse_error_frame_raises(monkeypatch):
    frame = {"jsonrpc": "2.0", "id": "1",
             "error": {"code": -32000, "message": "agent exploded"}}
    body = b"data: " + json.dumps(frame).encode() + b"\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body,
                              headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(a2a_client, "_new_client", lambda t: _mock_client(handler))
    with pytest.raises(RuntimeError, match="agent exploded"):
        await a2a_client.send_task_streaming(
            "https://x.example.com/a2a",
            [{"type": "text", "text": "hi"}], "agent-1",
        )


def test_parse_sse_frames_multiline():
    lines = ["data: {", "data: \"a\": 1}", "", "event: ping", "data: [DONE]"]
    frames = a2a_client._parse_sse_data_frames(lines)
    assert frames[0] == '{\n"a": 1}'
    assert frames[-1] == "[DONE]"
