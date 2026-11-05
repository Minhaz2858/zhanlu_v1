"""P2 Production Hardening — Phase 1 tests (Part 1.1 / 1.2 / 1.3).

Mock ONLY the LLM boundary (``call_llm_with_reliability``) and, where noted,
``record_invocation``. Real: Collaboration Broker, governance store,
authorization, Agent Task Contract, token-budget enforcement, true cancellation.

Run: pytest tests/test_p2_production_hardening.py
"""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

# record_invocation is imported locally inside delegate_tool._run_sub_agent, so to
# mock it we patch the source module (the local `from ... import` picks it up).
_REC_INV = "app.services.agent_invocations.record_invocation"

from unittest.mock import patch

import pytest

from app.config import settings
from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401
from app.models.agent_app import AgentApp

from app.services.tool_handlers import delegate_tool as dt  # noqa: E402
from app.services.agent_collaboration import broker  # noqa: E402
from app.services.agent_collaboration.governance_store import (  # noqa: E402
    get_store, reset_store, _InProcessStore, _RedisStore,
)


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _reset_state():
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
    db.add_all([
        AgentApp(name="Inventory Management Agent", description="stock",
                 status="published", org_id="default-org", app_id="default-app"),
    ])
    db.commit()


# ── 1.1 Distributed governance store (in-process fallback) ──────────────

def test_governance_store_inprocess():
    s = _InProcessStore()
    # Global slot cap
    assert s.acquire_global_slot(2) is True
    assert s.acquire_global_slot(2) is True
    assert s.acquire_global_slot(2) is False
    assert s.global_active_count() == 2
    s.release_global_slot()
    assert s.acquire_global_slot(2) is True
    # Dedup
    assert s.conv_dedup_seen("c1", "k1") is True
    assert s.conv_dedup_seen("c1", "k1") is False
    assert s.conv_dedup_seen("c1", "k2") is True
    # Child count
    assert s.conv_child_count("c1", "A") == 1
    assert s.conv_child_count("c1", "A") == 2
    assert s.conv_child_count("c1", "B") == 1
    # Token accumulation
    assert s.conv_token_add("c1", 10) == 10
    assert s.conv_token_add("c1", 5) == 15
    assert s.conv_token_total("c1") == 15


def test_governance_store_inprocess_isolated_ephemeral(db):
    # Calls without a conversation id are isolated per call (ephemeral).
    a = {"agent_name": "general_assistant"}
    b = {"agent_name": "general_assistant"}
    # Each call gets its own ephemeral namespace via the broker, so dedup
    # across two distinct calls does NOT collide (pre-audit semantics).
    broker.evaluate_delegation(target_agent_id="A", objective="same", context=a, db=db)
    ok2, _, _ = broker.evaluate_delegation(target_agent_id="A", objective="same", context=b, db=db)
    assert ok2 is True  # distinct ephemeral namespaces -> not a duplicate


# ── 1.1 Redis-backed store (skipped when Redis unavailable) ─────────────

def _redis_client_or_skip():
    try:
        from app.database import get_redis
        client = get_redis()
    except Exception:
        client = None
    if client is None:
        pytest.skip("Redis not available")
    # A client that connects but cannot authenticate (or fails a ping) is not a
    # usable Redis — skip rather than let every assertion fail-open.
    try:
        if not client.ping():
            pytest.skip("Redis not usable (ping returned falsy)")
    except Exception:
        pytest.skip("Redis not usable (auth/ping failed)")
    # Isolate: flush any collab keys.
    for k in client.scan_iter(match="collab:*"):
        client.delete(k)
    return client


def test_governance_store_redis():
    client = _redis_client_or_skip()
    s = _RedisStore(client)
    assert s.acquire_global_slot(1) is True
    assert s.acquire_global_slot(1) is False  # cap enforced across workers
    assert s.global_active_count() == 1
    s.release_global_slot()
    assert s.acquire_global_slot(1) is True
    s.release_global_slot()
    # Dedup (Redis SET)
    assert s.conv_dedup_seen("cr", "k1") is True
    assert s.conv_dedup_seen("cr", "k1") is False
    # Child count (Redis HINCRBY)
    assert s.conv_child_count("cr", "A") == 1
    assert s.conv_child_count("cr", "A") == 2
    # Tokens
    assert s.conv_token_add("cr", 7) == 7
    assert s.conv_token_total("cr") == 7
    for k in client.scan_iter(match="collab:*"):
        client.delete(k)


def test_governance_redis_lease_recovery():
    client = _redis_client_or_skip()
    s = _RedisStore(client)
    # Acquire with a 1s TTL; orphaned leases must expire (recovery).
    assert s.acquire_global_slot(4, ttl=1) is True
    assert s.global_active_count() == 1
    import time
    time.sleep(1.3)
    # The slot key expires; a fresh counter read shows 0 (recovery worked).
    assert s.global_active_count() == 0
    for k in client.scan_iter(match="collab:*"):
        client.delete(k)


# ── 1.2 True cancellation (timeout) ────────────────────────────────────

def test_true_cancellation_timeout(db, agents):
    async def _slow_inner(task, agent_name, db_, user_id_, max_iter, endpoint,
                          context=None, agent_app=None, wall_seconds=None):
        await asyncio.sleep(0.2)  # longer than the deadline
        return {"success": True, "response": "x", "iterations": 1}

    with patch.object(settings, "COLLAB_TIMEOUT_SECONDS", 0.01), \
         patch.object(dt, "_run_sub_agent_inner", side_effect=_slow_inner):
        res = asyncio.run(dt._run_sub_agent(
            "q", "Inventory Management Agent", db, "u1", 5, None,
            context={"agent_name": "general_assistant", "conversation_id": "ctimeout"},
        ))
    # The child coroutine is CANCELLED, not silently truncated.
    assert res["success"] is False
    assert res["status"] == "timeout"
    assert "deadline" in (res.get("error") or "").lower()


def test_parent_cancellation_propagates_and_records(db, agents):
    rec = {}

    async def _slow_inner(task, agent_name, db_, user_id_, max_iter, endpoint,
                          context=None, agent_app=None, wall_seconds=None):
        await asyncio.sleep(0.3)
        return {"success": True, "response": "x", "iterations": 1}

    def _fake_record(db, **kw):
        rec.update(kw)
        return None

    async def _run():
        return await dt._run_sub_agent(
            "q", "Inventory Management Agent", db, "u1", 5, None,
            context={"agent_name": "general_assistant", "conversation_id": "ccancel"},
        )

    async def _scenario():
        task = asyncio.ensure_future(_run())
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
            assert False, "expected CancelledError to propagate"
        except asyncio.CancelledError:
            pass

    with patch.object(dt, "_run_sub_agent_inner", side_effect=_slow_inner), \
         patch(_REC_INV, new=_fake_record):
        asyncio.run(_scenario())
    # Cleanup (finally) recorded the cancellation honestly, NOT as success.
    assert rec.get("status") == "cancelled"


# ── 1.3 Token / cost metering ──────────────────────────────────────────

def test_token_usage_surfaced_and_recorded(db, agents):
    async def _fake_llm(messages, tool_schemas, endpoint=None):
        return {
            "content": "answer", "tool_calls": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            "model": "gpt-4o",  # present in the known pricing table
        }

    rec = {}

    def _fake_record(db, **kw):
        rec.update(kw)
        return None

    with patch.object(dt, "call_llm_with_reliability", new=_fake_llm), \
         patch(_REC_INV, new=_fake_record):
        res = asyncio.run(dt._run_sub_agent(
            "q", "Inventory Management Agent", db, "u1", 5, None,
            context={"agent_name": "general_assistant", "conversation_id": "ctok"},
        ))
    assert res["success"] is True
    assert res["token_usage"]["total_tokens"] == 150
    assert res["token_usage"]["model"] == "gpt-4o"
    # The recorded invocation carries the same measured usage.
    assert rec.get("token_usage") == res["token_usage"]
    # gpt-4o is priced -> estimated cost is populated (measured pricing).
    assert rec.get("cost_amount") is not None and rec["cost_amount"] > 0


def test_token_usage_unknown_model_stays_unknown(db, agents):
    async def _fake_llm(messages, tool_schemas, endpoint=None):
        return {
            "content": "answer", "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "model": "some-unknown-zhanlu-model",  # NOT in pricing table
        }

    rec = {}

    def _fake_record(db, **kw):
        rec.update(kw)
        return None

    with patch.object(dt, "call_llm_with_reliability", new=_fake_llm), \
         patch(_REC_INV, new=_fake_record):
        res = asyncio.run(dt._run_sub_agent(
            "q", "Inventory Management Agent", db, "u1", 5, None,
            context={"agent_name": "general_assistant", "conversation_id": "ctok2"},
        ))
    assert res["token_usage"]["total_tokens"] == 15
    # Unknown model -> cost explicitly unknown (never invented).
    assert rec.get("cost_amount") is None


def test_token_budget_exhaustion_blocks_further_delegation(db):
    # Per-parent task budget of 100 tokens, with 100 already consumed in the
    # conversation store -> the next delegation must be blocked (completed
    # findings are preserved; the parent gets a partial result upstream).
    store = get_store()
    store.conv_token_add("cbudget", 100)
    ok, reason, _ = broker.evaluate_delegation(
        target_agent_id="Inventory Management Agent", objective="o",
        context={"agent_name": "general_assistant", "conversation_id": "cbudget",
                 "token_budget": 100}, db=db,
    )
    assert ok is False
    assert "token budget" in (reason or "").lower()
    assert "100" in (reason or "")
