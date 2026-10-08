"""Tests for the A2A outbound tool + external-agent registry (IBM #1/#2).

1. ExternalAgent model: serialization never leaks auth_token.
2. invoke_external_agent: registry resolution, delegation via the a2a
   client, disabled/unknown handling, payload extraction.
3. list_external_agents.
4. Well-known discovery path + X-API-Key machine auth on POST /a2a.
5. External-agents CRUD router basics.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base, engine, SessionLocal, get_db
import app.models  # noqa: F401  register all models
from app.models.external_agent import ExternalAgent


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


def _seed_external(db, name="partner-analyst", enabled=True, token=None):
    row = ExternalAgent(
        name=name,
        description="Partner data-analysis agent",
        endpoint_url="https://partner.example.com/a2a",
        auth_token=token,
        enabled=enabled,
        capabilities=["data-analysis"],
        granted_agents=None,
    )
    db.add(row)
    db.commit()
    return row


# ── Model ────────────────────────────────────────────────────────────────

def test_to_dict_never_leaks_auth_token(db):
    row = _seed_external(db, token="secret-token-123")
    d = row.to_dict()
    assert "secret-token-123" not in json.dumps(d)
    assert "auth_token" not in d
    assert d["endpoint_url"].startswith("https://")


# ── Tool: invoke_external_agent ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_invoke_resolves_registered_agent_and_delegates(db, monkeypatch):
    # unique name — the module shares one SQLite DB across tests
    row = _seed_external(db, name="invoke-analyst", token="tok-1")
    from app.services.tool_handlers import a2a_tool
    from app.services.a2a import client as a2a_client

    captured = {}

    async def fake_send(endpoint, message_parts, agent_id, token=None, **kw):
        captured.update(endpoint=endpoint, parts=message_parts, token=token)
        return {
            "id": "task-42",
            "status": {"state": "completed"},
            "artifacts": [
                {"parts": [{"type": "text", "text": "Analysis done: 42 lots"}]}
            ],
        }

    monkeypatch.setattr(a2a_client, "send_task", fake_send)
    result = await a2a_tool._invoke_external_agent(
        {"agent": "invoke-analyst", "task": "Count the lots"},
        db, "u1", None,
    )
    assert result["success"] is True
    assert captured["endpoint"] == "https://partner.example.com/a2a"
    assert captured["token"] == "tok-1"
    assert captured["parts"][0]["text"] == "Count the lots"
    assert result["text"] == "Analysis done: 42 lots"
    assert result["state"] == "completed"
    db.refresh(row)
    assert row.last_task_id == "task-42"


@pytest.mark.asyncio
async def test_invoke_disabled_agent_errors(db):
    _seed_external(db, name="disabled-agent", enabled=False)
    from app.services.tool_handlers import a2a_tool

    result = await a2a_tool._invoke_external_agent(
        {"agent": "disabled-agent", "task": "hi"}, db, "u1", None)
    assert result["success"] is False
    assert "disabled" in result["error"].lower()


@pytest.mark.asyncio
async def test_invoke_unknown_agent_without_url_errors(db):
    from app.services.tool_handlers import a2a_tool

    result = await a2a_tool._invoke_external_agent(
        {"agent": "no-such-agent", "task": "hi"}, db, "u1", None)
    assert result["success"] is False
    assert "list_external_agents" in result["error"]


@pytest.mark.asyncio
async def test_invoke_remote_failure_is_tool_result_not_crash(db, monkeypatch):
    _seed_external(db, name="flaky-agent")
    from app.services.tool_handlers import a2a_tool
    from app.services.a2a import client as a2a_client

    async def boom(*a, **k):
        raise RuntimeError("connect timeout")

    monkeypatch.setattr(a2a_client, "send_task", boom)
    result = await a2a_tool._invoke_external_agent(
        {"agent": "flaky-agent", "task": "hi"}, db, "u1", None)
    assert result["success"] is False
    assert "A2A delegation failed" in result["error"]


def test_extract_task_payload_reads_message_and_artifacts():
    from app.services.tool_handlers.a2a_tool import _extract_task_payload

    payload = _extract_task_payload({
        "id": "t1",
        "status": {"state": "working"},
        "message": {"parts": [{"text": "intermediate"}]},
        "artifacts": [{"parts": [{"text": "final"}]}],
    })
    assert payload["state"] == "working"
    assert "intermediate" in payload["text"] and "final" in payload["text"]


@pytest.mark.asyncio
async def test_list_external_agents_tool(db):
    _seed_external(db, name="listed-agent")
    from app.services.tool_handlers import a2a_tool

    result = await a2a_tool._list_external_agents({}, db, "u1", None)
    assert result["success"] is True
    names = [a["name"] for a in result["agents"]]
    assert "listed-agent" in names


def test_a2a_tools_registered_in_registry():
    from app.services.tool_registry import registry
    assert registry.get_entry("invoke_external_agent") is not None
    assert registry.get_entry("list_external_agents") is not None


# ── Well-known discovery + API-key auth ─────────────────────────────────

class _FakeExecutor:
    async def run(self, *, task, agent_app, db):
        from app.services.a2a import tasks as tasks_mod
        store = tasks_mod.A2aTaskStore(db)
        store.update_status(task, "completed", error=None)
        store.append_artifact(
            task, {"name": "reply", "mimeType": "text/plain", "text": "ok"})


@pytest.fixture
def api(db, monkeypatch):
    from app.routers import a2a as a2a_router

    app = FastAPI()
    app.include_router(a2a_router.router)
    app.include_router(a2a_router.wellknown_router)

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    fake = _FakeExecutor()
    monkeypatch.setattr(a2a_router.tasks_mod, "get_executor", lambda name="default": fake)
    return TestClient(app)


def test_wellknown_agent_card_served(api, db):
    from app.models.agent_app import AgentApp

    db.add(AgentApp(
        name="card-agent", description="d", prompt_identity="x",
        status="published", org_id="default-org", app_id="default-app",
    ))
    db.commit()
    resp = api.get("/.well-known/agent.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"]  # platform card name
    assert "skills" in card or "capabilities" in card


def test_api_key_auth_allows_machine_caller(api, db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "A2A_API_KEY", "sk-test-123")

    from app.models.agent_app import AgentApp
    agent = AgentApp(
        name="key-agent", description="d", prompt_identity="x",
        status="published", org_id="default-org", app_id="default-app",
    )
    db.add(agent)
    db.commit()

    resp = api.post(
        "/a2a",
        json={
            "jsonrpc": "2.0", "id": "r1", "method": "tasks/send",
            "params": {"agent_id": agent.id,
                       "message": {"parts": [{"text": "hi"}]}},
        },
        headers={"X-API-Key": "sk-test-123"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # task store serializes status as a plain state string
    assert body.get("result", {}).get("status") == "completed"


def test_api_key_wrong_and_missing_rejected(api, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "A2A_API_KEY", "sk-test-123")
    # wrong key
    r1 = api.post("/a2a", json={"jsonrpc": "2.0", "id": "r2",
                                "method": "tasks/get", "params": {"id": "x"}},
                  headers={"X-API-Key": "wrong"})
    assert r1.status_code == 401
    # no auth at all
    r2 = api.post("/a2a", json={"jsonrpc": "2.0", "id": "r3",
                                "method": "tasks/get", "params": {"id": "x"}})
    assert r2.status_code == 401


def test_no_api_key_configured_jwt_only(api, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "A2A_API_KEY", "")
    r = api.post("/a2a", json={"jsonrpc": "2.0", "id": "r4",
                               "method": "tasks/get", "params": {"id": "x"}},
                 headers={"X-API-Key": "anything"})
    assert r.status_code == 401


# ── Registry CRUD router ────────────────────────────────────────────────

@pytest.fixture
def crud_client(db, monkeypatch):
    from app.routers import external_agents as ea
    from app.deps import get_current_user_required

    app = FastAPI()
    app.include_router(ea.router)

    def _override_db():
        yield db

    class _FakeUser:
        id = "u1"

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_required] = lambda: _FakeUser()
    return TestClient(app)


def test_crud_create_list_delete_roundtrip(crud_client, db):
    created = crud_client.post("/external-agents", json={
        "name": "roundtrip-agent",
        "endpoint_url": "https://x.example.com/a2a",
        "auth_token": "tok-xyz",
        "capabilities": ["analysis"],
    })
    assert created.status_code == 200
    row_id = created.json()["id"]
    assert "tok-xyz" not in created.text  # token never echoed

    listed = crud_client.get("/external-agents")
    assert any(r["id"] == row_id for r in listed.json())

    deleted = crud_client.delete(f"/external-agents/{row_id}")
    assert deleted.json()["ok"] is True
    names = [r["name"] for r in crud_client.get("/external-agents").json()]
    assert "roundtrip-agent" not in names


def test_crud_rejects_bad_url(crud_client):
    r = crud_client.post("/external-agents", json={
        "name": "bad", "endpoint_url": "ftp://nope"})
    assert r.status_code == 400
