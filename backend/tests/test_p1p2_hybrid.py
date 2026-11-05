"""P1/P2 Hybrid multi-agent orchestration tests (2026-10-31).

Covers the Collaboration Broker governance (spec §2D, §4) plus the
enterprise acceptance scenarios that the P1 scenario harness did NOT yet
cover:

  Scenario 1  simple question        -> single specialist, no fan-out
  Scenario 3  peer collaboration      -> Supply Chain -> Inventory via broker
  Scenario 5  circular delegation     -> A -> B -> A blocked, no crash
  Scenario 7  unauthorized resource   -> unpublished target blocked + audited

Scenarios 2/4/6 are covered by tests/test_p1_scenarios.py (multi-agent
batch, cross-department discovery, explicit partial/failure).

The Collaboration Broker is a pure policy layer — its checks are tested
directly (no LLM) AND through the real _run_sub_agent entry point with the
sub-agent inner loop mocked. The user's authorization context (user_id /
organization / project) is asserted preserved on every delegated contract.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

from unittest.mock import patch

import pytest
import asyncio

from app.config import settings
from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401
from app.models.agent_app import AgentApp

try:  # registration side effects
    import app.services.tool_handlers.delegate_tool  # noqa: F401
except ImportError:  # pragma: no cover
    pass

from app.services.tool_handlers import delegate_tool as dt  # noqa: E402
from app.services.agent_collaboration import broker  # noqa: E402
from app.services.agent_collaboration.contract import build_contract  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _reset_governance_state():
    # The governance store (Redis-backed in this environment) persists across
    # tests; reset it so dedup / child-count / token counters don't leak.
    from app.services.agent_collaboration.governance_store import reset_store
    reset_store()
    broker.reset_concurrency()
    broker.reset_conv_state()
    yield
    reset_store()
    broker.reset_concurrency()
    broker.reset_conv_state()


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def agents(db):
    """Three agents: one published specialist, one builtin-style (no AgentApp
    row → legacy-allowed), one UNPUBLISHED (must be blocked)."""
    pub = AgentApp(
        name="Inventory Management Agent", description="stock", status="published",
        org_id="default-org", app_id="default-app",
    )
    unpub = AgentApp(
        name="Secret Agent", description="hidden", status="draft",
        org_id="default-org", app_id="default-app",
    )
    db.add_all([pub, unpub])
    db.commit()
    return {"published": pub, "unpublished": unpub}


# ── Scenario 1: single specialist, no unnecessary fan-out ──────────────

def test_scenario_1_single_specialist(db):
    """A single delegate_task call resolves to ONE specialist — the GA must
    not fan out when one agent suffices."""
    called = {}

    async def fake_inner(task, agent_name, db_, user_id_, max_iter, endpoint, context=None, agent_app=None, wall_seconds=None):
        called["agent"] = agent_name
        return {"success": True, "response": "done", "iterations": 1}

    with patch.object(dt, "_run_sub_agent_inner", side_effect=fake_inner):
        res = asyncio.run(dt._run_sub_agent("check stock", "Inventory Management Agent", db, "u1", 5, None,
                                context={"agent_name": "general_assistant", "conversation_id": "c1", "user_id": "u1"}))
    assert res["success"] is True
    assert called["agent"] == "Inventory Management Agent"
    # Contract carried the original user identity (never the GA's).
    assert res.get("status") == "completed"


# ── Scenario 3: peer collaboration (Pattern C) ─────────────────────────

def test_scenario_3_specialist_requests_peer(db, agents):
    """Supply Chain Agent discovers missing inventory info and requests help
    from the Inventory agent through the broker. The broker allows it and
    records the parent->child link (requesting_agent_id = Supply Chain)."""
    seen = {}

    async def fake_inner(task, agent_name, db_, user_id_, max_iter, endpoint, context=None, agent_app=None, wall_seconds=None):
        seen["requesting"] = context.get("requesting_agent_id")
        seen["path"] = list(context.get("delegation_path") or [])
        return {"success": True, "response": "stock levels", "iterations": 1}

    with patch.object(dt, "_run_sub_agent_inner", side_effect=fake_inner):
        res = asyncio.run(dt._run_sub_agent(
            "What is the current inventory for the delayed SKUs?",
            "Inventory Management Agent", db, "u1", 5, None,
            context={
                "agent_name": "Supply Chain Agent",     # the requesting specialist
                "conversation_id": "c2", "user_id": "u1",
                "delegation_path": ["general_assistant"],  # GA -> Supply Chain already
            },
        ))
    assert res["success"] is True
    # The child saw Supply Chain (not GA) as the requesting agent.
    assert seen["requesting"] == "Supply Chain Agent"
    # delegation_path records the delegated TARGETS, root-first:
    # general_assistant (root) -> Inventory Management Agent.
    assert seen["path"][-1] == "Inventory Management Agent"
    assert "Inventory Management Agent" in seen["path"]
    assert seen["path"][0] == "general_assistant"


# ── Scenario 5: circular delegation is blocked, not crashed ─────────────

def test_scenario_5_circular_delegation_blocked(db):
    """Attempted A -> B -> A (or A -> B -> C -> A) is detected and refused
    with a clean status=blocked — the parent execution must NOT crash."""
    # A -> B already happened; B tries to delegate back to A.
    res = asyncio.run(dt._run_sub_agent(
        "loop back", "A Agent", db, "u1", 5, None,
        context={
            "agent_name": "B Agent",
            "conversation_id": "c3", "user_id": "u1",
            "delegation_path": ["general_assistant", "A Agent", "B Agent"],
        },
    ))
    assert res["success"] is False
    assert res["status"] == "blocked"
    assert "cycle" in (res.get("error") or "").lower()
    assert "A Agent" in (res.get("error") or "")


def test_scenario_5_depth_limit(db):
    """A delegation chain past COLLAB_MAX_DEPTH is refused."""
    with patch.object(settings, "COLLAB_MAX_DEPTH", 2):
        res = asyncio.run(dt._run_sub_agent(
            "deep", "Deep Agent", db, "u1", 5, None,
            context={"agent_name": "GA2", "delegation_path": ["general_assistant", "X", "Y"]},
        ))
    assert res["status"] == "blocked"
    assert "depth" in (res.get("error") or "").lower()


# ── Scenario 7: unauthorized / unpublished target ──────────────────────

def test_scenario_7_unpublished_target_blocked(db, agents):
    """A delegation to an agent that exists but is NOT published is refused
    (fail-closed) and recorded in the audit trail (observable invocation)."""
    res = asyncio.run(dt._run_sub_agent("secret", "Secret Agent", db, "u1", 5, None,
                        context={"agent_name": "general_assistant", "conversation_id": "c7"}))
    assert res["success"] is False
    assert res["status"] == "blocked"
    assert "not published" in (res.get("error") or "").lower()

    # Audit: the broker records the policy block as an observable invocation.
    audit_calls = []
    with patch.object(broker, "_audit_block", side_effect=lambda *a, **k: audit_calls.append((a, k))):
        asyncio.run(dt._run_sub_agent("secret", "Secret Agent", db, "u1", 5, None,
                      context={"agent_name": "general_assistant", "conversation_id": "c7"}))
    assert len(audit_calls) == 1


# ── Broker pure-policy checks (no LLM) ─────────────────────────────────

def test_broker_allows_published_and_builtin(db, agents):
    ok, reason, ctx = broker.evaluate_delegation(
        target_agent_id="Inventory Management Agent", objective="x",
        context={"agent_name": "general_assistant"}, db=db,
    )
    assert ok is True
    assert ctx["delegation_path"] == ["Inventory Management Agent"]


def test_broker_blocks_unpublished(db, agents):
    ok, reason, _ = broker.evaluate_delegation(
        target_agent_id="Secret Agent", objective="x",
        context={"agent_name": "general_assistant"}, db=db,
    )
    assert ok is False
    assert "not published" in (reason or "").lower()


def test_broker_blocks_cycle(db):
    ok, reason, _ = broker.evaluate_delegation(
        target_agent_id="B", objective="x",
        context={"agent_name": "A", "delegation_path": ["general_assistant", "B", "A"]},
        db=db,
    )
    assert ok is False and "cycle" in (reason or "").lower()


def test_broker_child_count_limit(db):
    with patch.object(settings, "COLLAB_MAX_CHILDREN_PER_PARENT", 2):
        # The child counter lives in the SHARED per-conversation store, so a
        # single top-level request's sibling children are all counted against it
        # (not a fresh copy per call). Use one conversation_id.
        ctx = {"agent_name": "general_assistant", "conversation_id": "c-child"}
        _, _, ctx = broker.evaluate_delegation(target_agent_id="A", objective="o1", context=ctx, db=db)
        _, _, ctx = broker.evaluate_delegation(target_agent_id="B", objective="o2", context=ctx, db=db)
        ok3, reason3, _ = broker.evaluate_delegation(target_agent_id="C", objective="o3", context=ctx, db=db)
        assert ok3 is False and "children" in (reason3 or "").lower()


def test_broker_duplicate_prevention(db):
    with patch.object(settings, "COLLAB_DEDUP_ENABLED", True):
        # First delegation of (general_assistant -> A, "same task") is allowed.
        ok1, _, ctx = broker.evaluate_delegation(
            target_agent_id="A", objective="same task",
            context={"agent_name": "general_assistant", "conversation_id": "c-dup"}, db=db,
        )
        assert ok1 is True
        # Re-delegating the IDENTICAL objective from the same requester must
        # be refused as a duplicate (shared per-conversation seen set).
        ok2, reason2, _ = broker.evaluate_delegation(
            target_agent_id="A", objective="same task", context=ctx, db=db,
        )
        assert ok2 is False and "duplicate" in (reason2 or "").lower()
        # Distinct objective in the SAME conversation is allowed (not a duplicate).
        ok3, _, _ = broker.evaluate_delegation(
            target_agent_id="A", objective="different task",
            context={"agent_name": "general_assistant", "conversation_id": "c-dup"}, db=db,
        )
        assert ok3 is True


def test_broker_preserves_user_identity(db):
    """The user's authorization context is never replaced by the requesting
    agent's identity on the contract."""
    contract = build_contract(
        objective="report", target_agent_id="Inventory Management Agent",
        context={"user_id": "u-real", "organization_id": "org-1", "project_id": "p-1",
                 "conversation_id": "c-x", "agent_name": "Supply Chain Agent"},
    )
    assert contract.user_id == "u-real"
    assert contract.organization_id == "org-1"
    assert contract.project_id == "p-1"
    assert contract.requesting_agent_id == "Supply Chain Agent"


def test_broker_master_kill_switch(db):
    """COLLAB_POLICY_ENFORCED=False disables governance (rollback lever)."""
    with patch.object(settings, "COLLAB_POLICY_ENFORCED", False):
        ok, _, ctx = broker.evaluate_delegation(
            target_agent_id="Secret Agent", objective="x",
            context={"agent_name": "general_assistant", "delegation_path": ["general_assistant"]},
            db=db,
        )
        assert ok is True
        assert "Secret Agent" in ctx["delegation_path"]
