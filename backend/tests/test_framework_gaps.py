"""Tests for the IBM framework-audit gap fixes (2026-10-08).

#3 credential encryption at rest (crypto + ExternalAgent + MCP oauth)
#1 group-chat pattern (swarm_group_chat)
#2 run replay (executions/{id}/replay)
#4 OTel collector config (file presence + env wiring documented)
"""
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.database import Base, engine, SessionLocal, get_db
import app.models  # noqa: F401
from app.models.agent_app import AgentApp
from app.models.execution import Execution, Plan, PlanNode
from app.models.external_agent import ExternalAgent


@pytest.fixture(scope="module", autouse=True)
def _tables():
    # Resolve the engine at FIXTURE time: conftest's session fixture replaces
    # app.database.engine with a StaticPool variant, and a module-level import
    # would bind the pre-replacement engine (whose in-memory DB is discarded).
    import app.database as appdb

    Base.metadata.create_all(appdb.engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ── #3 encryption at rest ───────────────────────────────────────────────

def test_crypto_roundtrip_and_prefix():
    from app.services.crypto import encrypt_value, decrypt_value, is_encrypted

    secret = "sk-super-secret-token"
    stored = encrypt_value(secret)
    assert stored != secret
    assert is_encrypted(stored)
    assert decrypt_value(stored) == secret


def test_crypto_legacy_plaintext_passthrough():
    from app.services.crypto import decrypt_value

    assert decrypt_value("legacy-plain-token") == "legacy-plain-token"
    assert decrypt_value(None) is None


def test_crypto_derives_key_from_jwt_secret():
    """Empty CREDENTIAL_ENCRYPTION_KEY → derived from JWT_SECRET, still round-trips."""
    from app.services import crypto

    token = crypto.encrypt_value("value-1")
    assert crypto.decrypt_value(token) == "value-1"


def test_external_agent_token_encrypted_at_rest(db):
    from app.services.crypto import decrypt_value, is_encrypted

    row = ExternalAgent(
        name="enc-agent",
        endpoint_url="https://x.example.com/a2a",
        auth_token=None,
    )
    from app.services.crypto import encrypt_value
    row.auth_token = encrypt_value("bearer-secret-1")
    db.add(row)
    db.commit()
    db.refresh(row)
    assert is_encrypted(row.auth_token)
    assert "bearer-secret-1" not in (row.auth_token or "")
    assert decrypt_value(row.auth_token) == "bearer-secret-1"


def test_mcp_oauth_tokens_encrypted_in_json():
    from app.services.mcp_client import oauth as oauth_mod
    from app.services.crypto import is_encrypted, decrypt_value

    tokens = {"access_token": "at-xyz", "token_type": "Bearer"}
    raw = oauth_mod.tokens_to_json(tokens)
    assert is_encrypted(raw)
    assert "at-xyz" not in raw
    assert oauth_mod.tokens_from_json(raw) == tokens
    # legacy plaintext JSON still readable
    legacy = json.dumps(tokens)
    assert oauth_mod.tokens_from_json(legacy) == tokens


# ── #1 group chat ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_group_chat_rounds_and_completion(db):
    from app.services.swarm import group_chat as gc
    from app.services.swarm.team_registry import get_team_registry

    registry = get_team_registry()
    team = registry.create_team("gc-test-team", description="t")
    replies = {
        "plan": "I propose: fetch the data, then verify it.",
        "worker": "Data fetched: 42 rows.",
        "verification": "[COMPLETE] All checks passed: 42 rows verified.",
    }

    class _FakeResult:
        def __init__(self, name):
            self.final_response = replies[name]
            self.success = True
            self.error = None

    class _FakeCoordinator:
        async def run_task(self, team_id, agent_name, task, member_name=None,
                           db=None, user_id=None):
            return _FakeResult(agent_name)

    result = await gc.run_group_chat(
        _FakeCoordinator(), registry, team.id,
        "Verify the inventory count", ["plan", "worker", "verification"],
        max_rounds=6, speaker_policy="round_robin", db=db, user_id="u1",
    )
    assert result["success"] is True
    assert result["completed"] is True
    assert result["completed_by"] == "verification"
    speakers = [t["speaker"] for t in result["transcript"]]
    assert speakers == ["plan", "worker", "verification"]  # round-robin order
    # shared thread: every agent's reply landed in the team mailbox
    # (recipient="all" is the shared thread — see group_chat._thread_transcript)
    thread = registry.get_messages(team.id, "all", limit=20)
    assert any("42 rows" in str(m.get("content", "")) for m in thread)


@pytest.mark.asyncio
async def test_group_chat_max_rounds_bound(db):
    from app.services.swarm import group_chat as gc
    from app.services.swarm.team_registry import get_team_registry

    registry = get_team_registry()
    team = registry.create_team("gc-rounds-team", description="t")

    class _FakeResult:
        final_response = "still working…"  # never completes
        success = True
        error = None

    class _FakeCoordinator:
        async def run_task(self, *a, **k):
            return _FakeResult()

    result = await gc.run_group_chat(
        _FakeCoordinator(), registry, team.id, "endless topic",
        ["plan", "worker"], max_rounds=3, speaker_policy="round_robin")
    assert result["rounds_used"] == 3
    assert result["completed"] is False


@pytest.mark.asyncio
async def test_group_chat_requires_two_agents():
    from app.services.swarm import group_chat as gc
    from app.services.swarm.team_registry import get_team_registry

    registry = get_team_registry()
    team = registry.create_team("gc-one-team", description="t")

    class _FakeCoordinator:
        async def run_task(self, *a, **k):  # pragma: no cover
            raise AssertionError("should not run")

    result = await gc.run_group_chat(
        _FakeCoordinator(), registry, team.id, "t", ["plan"],
        max_rounds=2)
    assert result["success"] is False


def test_swarm_group_chat_tool_registered():
    from app.services.tool_handlers import swarm_tools  # noqa: F401
    from app.services.tool_registry import registry

    assert registry.get_entry("swarm_group_chat") is not None


# ── #2 run replay ───────────────────────────────────────────────────────

@pytest.fixture
def exec_client(db, monkeypatch):
    from app.routers import executions as ex
    from app.deps import get_current_user_required

    app = FastAPI()
    app.include_router(ex.router)

    def _override_db():
        yield db

    class _U:
        id = "u1"

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_required] = lambda: _U()
    return TestClient(app)


def test_replay_clones_plan_frozen(exec_client, db):
    execution = Execution(user_message="original ask", agent_name="general_assistant")
    db.add(execution)
    db.commit()
    plan = Plan(execution_id=execution.id, version=1, status="completed",
                summary="original")
    db.add(plan)
    db.commit()
    db.add(PlanNode(plan_id=plan.id, seq=1, node_type="tool",
                    name="step_one", dependencies=[],
                    inputs={"tool_name": "noop"}))
    db.add(PlanNode(plan_id=plan.id, seq=2, node_type="tool",
                    name="step_two", dependencies=[1],
                    inputs={"tool_name": "noop"}))
    db.commit()

    resp = exec_client.post(f"/executions/{execution.id}/replay",
                            json={"notes": "test replay"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "frozen"
    assert body["nodes_cloned"] == 2
    assert body["execution_id"] != execution.id

    clone = db.query(Execution).filter(Execution.id == body["execution_id"]).first()
    assert clone.mode == "frozen"
    assert clone.user_message == "original ask"
    clone_plan = db.query(Plan).filter(Plan.id == body["plan_id"]).first()
    assert clone_plan.status == "approved"  # frozen replays skip the GATE
    cloned_nodes = sorted(clone_plan.nodes, key=lambda n: n.seq)
    assert [n.name for n in cloned_nodes] == ["step_one", "step_two"]
    assert cloned_nodes[1].dependencies == [1]  # dependency topology preserved
    # all fresh state
    assert all(n.status == "pending" for n in cloned_nodes)


def test_replay_without_plan_422(exec_client, db):
    execution = Execution(user_message="no plan", agent_name="general_assistant")
    db.add(execution)
    db.commit()
    resp = exec_client.post(f"/executions/{execution.id}/replay", json={})
    assert resp.status_code == 422


def test_replay_unknown_404(exec_client):
    resp = exec_client.post("/executions/nope/replay", json={})
    assert resp.status_code == 404


# ── #4 OTel collector config ────────────────────────────────────────────

def test_otel_collector_sample_config_exists():
    import pathlib

    # backend/tests/x.py → parents[1] = backend/ (mounted as /app in containers)
    cfg = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "otel-collector-config.yaml"
    assert cfg.exists()
    text = cfg.read_text()
    assert "otlp" in text and "4318" in text
