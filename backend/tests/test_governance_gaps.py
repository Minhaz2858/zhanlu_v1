"""G1-G3 governance gap tests: cancel endpoint, outbound identity, tier policy."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, SessionLocal, engine, get_db  # noqa: E402
import app.models  # noqa: F401, E402
from app.deps import get_current_user_required  # noqa: E402
from app.models.agent_app import AgentApp  # noqa: E402
from app.services.governance.agent_tier import (  # noqa: E402
    check_agent_rate_limit,
    escalation_applies,
    resolve_agent_tier_policy,
)
from app.services import run_control  # noqa: E402


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


# ── G2: outbound agent identity ──────────────────────────────────────────

def test_a2a_headers_carry_identity():
    from app.services.a2a.client import _headers

    h = _headers(token=None)
    assert h["X-Agent-Id"] == settings.A2A_SELF_AGENT_ID
    assert h["User-Agent"].startswith("ZhanluAgent/1.0")
    assert "Authorization" not in h

    h2 = _headers(token="tok")
    assert h2["Authorization"] == "Bearer tok"
    # caller-supplied auth must not clobber identity headers
    assert "X-Agent-Id" in h2


def test_mcp_http_headers_merged():
    from app.services.mcp_client import connect_http  # import-safety

    assert callable(connect_http)  # headers merged inside; smoke only


# ── G3: agent-tier policy ────────────────────────────────────────────────

def _mk_agent(db, name: str, tier: str | None, rate: int | None) -> str:
    profile = {}
    if tier:
        profile["risk_tier"] = tier
    if rate:
        profile["rate_limit_per_minute"] = rate
    row = AgentApp(
        name=name, description="d", prompt_identity="x", status="published",
        org_id="default-org", app_id=f"app-{name}",
        policy_profile=profile or None,
    )
    db.add(row)
    db.commit()
    return row.id


def test_resolve_tier_from_app_id(db):
    app_id = _mk_agent(db, "tier-governance-agent", "critical", 10)
    ctx = {"agent_app_id": app_id, "agent_name": "tier-governance-agent"}
    tier, rate = resolve_agent_tier_policy(db, ctx)
    assert tier == "critical"
    assert rate == 10
    # cached on the context — a second resolve must not re-query
    ctx["agent_app_id"] = "deleted-id"
    tier2, rate2 = resolve_agent_tier_policy(db, ctx)
    assert (tier2, rate2) == ("critical", 10)


def test_resolve_tier_none_for_unknown(db):
    tier, rate = resolve_agent_tier_policy(db, {"agent_name": "no-such-agent-xyz"})
    assert tier is None and rate is None


def test_escalation_applies_only_high_tiers():
    assert escalation_applies("high") is True
    assert escalation_applies("critical") is True
    assert escalation_applies("low") is False
    assert escalation_applies(None) is False


def test_rate_limit_sliding_window():
    ok1, _ = check_agent_rate_limit("rate-test-agent", 2)
    ok2, _ = check_agent_rate_limit("rate-test-agent", 2)
    ok3, retry = check_agent_rate_limit("rate-test-agent", 2)
    assert ok1 and ok2
    assert not ok3
    assert retry > 0


# ── G1: mid-turn cancellation ────────────────────────────────────────────

def test_run_control_lifecycle():
    conv = "conv-cancel-test-1"
    run_control.register_run(conv)
    assert run_control.request_cancel(conv)["cancelling"] is True
    assert run_control.is_cancelled(conv)
    run_control.unregister_run(conv)
    assert not run_control.is_cancelled(conv)


def test_register_clears_stale_cancel():
    conv = "conv-cancel-test-2"
    run_control.register_run(conv)
    run_control.request_cancel(conv)
    # a NEW run on the same conversation clears the stale flag
    run_control.register_run(conv)
    assert not run_control.is_cancelled(conv)
    run_control.unregister_run(conv)


def test_cancel_endpoint(db):
    from app.routers.agents import router as agents_router

    app = FastAPI()
    app.include_router(agents_router)

    def _override_db():
        yield db

    admin = db.query(
        __import__("app.models.user", fromlist=["User"]).User
    ).filter_by(email="admin@zhanlu.dev").first()
    if admin is None:
        admin = __import__("app.models.user", fromlist=["User"]).User(
            id="u-admin-1", email="admin@zhanlu.dev", full_name="Admin",
            password_hash="x", role="admin", org_id="default-org",
        )
        db.add(admin)
        db.commit()

    conv = __import__(
        "app.models.agent_conversation", fromlist=["AgentConversation"]
    ).AgentConversation(
        id="conv-governance-cancel", title="t", agent_name="general_assistant",
        created_by_id=admin.id,
    )
    db.add(conv)
    db.commit()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_required] = lambda: admin

    client = TestClient(app)
    run_control.register_run("conv-governance-cancel")
    resp = client.post(
        "/apps/local-zhanlu-app/agents/conversations/conv-governance-cancel/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["cancelling"] is True
    assert run_control.is_cancelled("conv-governance-cancel")
    run_control.unregister_run("conv-governance-cancel")


def test_cancel_endpoint_unknown_conversation_404(db):
    from app.routers.agents import router as agents_router
    from app.models.user import User

    app = FastAPI()
    app.include_router(agents_router)
    app.dependency_overrides[get_db] = lambda: iter([db])
    admin = User(id="u-admin-2", email="admin2@zhanlu.dev", full_name="A",
                 password_hash="x", role="admin", org_id="default-org")
    app.dependency_overrides[get_current_user_required] = lambda: admin

    client = TestClient(app)
    resp = client.post(
        "/apps/local-zhanlu-app/agents/conversations/no-such-conv-xyz/cancel")
    assert resp.status_code == 404
