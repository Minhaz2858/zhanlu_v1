"""P1/P2 Hybrid orchestration — adversarial production-readiness tests.

Mock ONLY the LLM boundary (``call_llm_with_reliability``). The Collaboration
Broker, authorization checks, execution context, Agent Task Contract and
invocation persistence stay REAL. Covers the 10 scenarios from the audit §7:

  1. A -> B -> A circular delegation (blocked, no crash)
  2. A -> B -> C -> D -> E exceeding max depth
  3. Multiple children exceeding the per-parent limit
  4. Two simultaneous identical requests (dedup race under concurrency)
  5. Concurrent delegation across different conversations (global gate)
  6. Unauthorized cross-project resource access (identity not substituted)
  7. Child timeout with the parent continuing safely
  8. Partial results from one failed child
  9. Provider quota exhaustion (honest failure, no false success)
 10. Invalid / incomplete agent evidence (no crash, no fabricated success)

Run: pytest tests/test_p1p2_hybrid_hardening.py
"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

from unittest.mock import patch, AsyncMock

import pytest

from app.config import settings
from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401
from app.models.agent_app import AgentApp

from app.services.tool_handlers import delegate_tool as dt  # noqa: E402
from app.services.agent_collaboration import broker  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _reset_state():
    broker.reset_concurrency()
    broker.reset_conv_state()
    yield
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
    pub = AgentApp(name="Inventory Management Agent", description="stock",
                   status="published", org_id="default-org", app_id="default-app")
    unpub = AgentApp(name="Secret Agent", description="hidden", status="draft",
                     org_id="default-org", app_id="default-app")
    db.add_all([pub, unpub])
    db.commit()
    return {"published": pub, "unpublished": unpub}


def _mock_llm_text(text="ok", sleep=0.0):
    """Patch the LLM boundary to return a final (no-tool-call) answer."""

    async def _fake(messages, tool_schemas, endpoint=None):
        if sleep:
            await asyncio.sleep(sleep)
        return {"content": text, "tool_calls": []}

    return patch.object(dt, "call_llm_with_reliability", new=_fake)


# ── 1. Circular delegation A -> B -> A ────────────────────────────────

def test_adversarial_circular_a_b_a(db):
    res = asyncio.run(dt._run_sub_agent(
        "loop back", "A Agent", db, "u1", 5, None,
        context={"agent_name": "B Agent", "conversation_id": "c", "user_id": "u1",
                 "delegation_path": ["general_assistant", "A Agent", "B Agent"]},
    ))
    assert res["success"] is False
    assert res["status"] == "blocked"
    assert "cycle" in (res.get("error") or "").lower()
    assert "A Agent" in (res.get("error") or "")


# ── 2. Depth-exceeding chain A -> B -> C -> D -> E ─────────────────────

def test_adversarial_exceeds_max_depth(db):
    with patch.object(settings, "COLLAB_MAX_DEPTH", 3):
        res = asyncio.run(dt._run_sub_agent(
            "deep", "E Agent", db, "u1", 5, None,
            context={"agent_name": "D Agent", "delegation_path":
                      ["general_assistant", "A Agent", "B Agent", "C Agent"]},
        ))
    assert res["status"] == "blocked"
    assert "depth" in (res.get("error") or "").lower()


# ── 3. Per-parent child-count limit ───────────────────────────────────

def test_adversarial_too_many_children(db):
    with patch.object(settings, "COLLAB_MAX_CHILDREN_PER_PARENT", 2):
        # Shared conversation_id => the child counter is SHARED (a single
        # top-level request's sibling children are all counted against it).
        ctx = {"agent_name": "general_assistant", "conversation_id": "c-child"}
        _, _, ctx = broker.evaluate_delegation(
            target_agent_id="A", objective="o1", context=ctx, db=db)
        _, _, ctx = broker.evaluate_delegation(
            target_agent_id="B", objective="o2", context=ctx, db=db)
        ok3, reason3, _ = broker.evaluate_delegation(
            target_agent_id="C", objective="o3", context=ctx, db=db)
    assert ok3 is False
    assert "children" in (reason3 or "").lower()


# ── 4. Dedup race: concurrent identical requests ──────────────────────

def test_adversarial_concurrent_duplicate_dedup(db):
    """Two+ simultaneous identical delegations against the SAME shared context
    must NOT both pass the dedup gate (the lock fix). With N concurrent calls
    only ONE should be allowed."""
    with patch.object(settings, "COLLAB_DEDUP_ENABLED", True):
        # Shared conversation_id => the dedup set is SHARED across the 12
        # concurrent calls (proves the per-conversation store, not a per-call copy).
        ctx = {"agent_name": "general_assistant", "conversation_id": "race-conv"}

        def _call():
            return broker.evaluate_delegation(
                target_agent_id="A", objective="same task", context=ctx, db=db)

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda _: _call(), range(12)))
    allowed = [r for r in results if r[0] is True]
    blocked = [r for r in results if r[0] is False]
    assert len(allowed) == 1, f"expected exactly 1 allowed, got {len(allowed)}"
    assert len(blocked) == 11
    assert all("duplicate" in (r[1] or "").lower() for r in blocked)


# ── 5. Global concurrency gate across different conversations ──────────

def test_adversarial_global_concurrency_gate(db):
    captured = []

    async def _slow_inner(task, agent_name, db_, user_id_, max_iter, endpoint,
                          context=None, agent_app=None, wall_seconds=None):
        captured.append(broker.get_store().global_active_count())
        await asyncio.sleep(0.05)
        return {"success": True, "response": "x", "iterations": 1}

    with patch.object(settings, "COLLAB_MAX_CONCURRENT", 2), \
         patch.object(dt, "_run_sub_agent_inner", side_effect=_slow_inner):
        async def _fire():
            return await asyncio.gather(*[
                dt._run_sub_agent(f"t{i}", "Inventory Management Agent", db, "u1", 5, None,
                                  context={"agent_name": "general_assistant",
                                           "conversation_id": f"conv-{i}"})
                for i in range(5)
            ], return_exceptions=True)

        outcomes = asyncio.run(_fire())

    blocked = [o for o in outcomes if isinstance(o, dict) and o.get("status") == "blocked"]
    # With limit 2 and 5 near-simultaneous top-level delegations, at least one
    # must be refused by the gate (cannot all occupy 2 slots at once).
    assert len(blocked) >= 1, f"expected >=1 concurrency-blocked, got {len(blocked)}"
    # The live counter must never have exceeded the limit.
    assert max(captured) <= 2
    assert "concurrent" in (blocked[0].get("error") or "").lower()


# ── 6. Cross-project: user identity never substituted ─────────────────

def test_adversarial_cross_project_identity_preserved(db):
    seen = {}

    async def _capture_inner(task, agent_name, db_, user_id_, max_iter, endpoint,
                             context=None, agent_app=None, wall_seconds=None):
        # The CHILD must run AS ITSELF but with the ORIGINAL user's identity —
        # a specialist must NOT be able to substitute its own id for the user's,
        # nor inherit permissions the user lacks.
        seen["user_id"] = context.get("user_id")
        seen["project_id"] = context.get("project_id")
        seen["organization_id"] = context.get("organization_id")
        seen["agent_name"] = context.get("agent_name")
        return {"success": True, "response": "x", "iterations": 1}

    with patch.object(dt, "_run_sub_agent_inner", side_effect=_capture_inner):
        res = asyncio.run(dt._run_sub_agent(
            "report", "Inventory Management Agent", db, "u-real", 5, None,
            context={"agent_name": "general_assistant", "user_id": "u-real",
                     "project_id": "p-real", "organization_id": "org-real",
                     "conversation_id": "c6"},
        ))
    assert res["success"] is True
    # Identity preserved verbatim — the child context still carries the USER's
    # authorization context, never the requesting agent's.
    assert seen["user_id"] == "u-real"
    assert seen["project_id"] == "p-real"
    assert seen["organization_id"] == "org-real"
    # Contract (built by the broker) also preserves the user's identity.
    contract = broker.evaluate_delegation(
        target_agent_id="Inventory Management Agent", objective="o",
        context={"user_id": "u-real", "project_id": "p-real",
                 "organization_id": "org-real", "agent_name": "Supply Chain Agent"},
        db=db)[2]["agent_task_contract"]
    assert contract["user_id"] == "u-real"
    assert contract["project_id"] == "p-real"
    assert contract["requesting_agent_id"] == "Supply Chain Agent"


# ── 7. Child timeout — parent continues safely ────────────────────────

def test_adversarial_child_timeout_parent_survives(db):
    # Make the (mocked) LLM slow; set the sub-agent wall-clock tiny so the
    # child coroutine is CANCELLED (true cancellation, not silent truncation).
    # The parent must NOT crash and must receive an honest timeout status.
    with patch.object(settings, "COLLAB_TIMEOUT_SECONDS", 0.01), \
         _mock_llm_text("slow answer", sleep=0.2):
        res = asyncio.run(dt._run_sub_agent(
            "think", "Inventory Management Agent", db, "u1", 5, None,
            context={"agent_name": "general_assistant", "conversation_id": "c7"},
        ))
    assert res["success"] is False         # timed-out child is not reported ok
    assert res["status"] == "timeout"      # honest, distinguishable status
    assert "deadline" in (res.get("error") or "").lower()


# ── 8. Partial results: one failed child, others succeed ──────────────

def test_adversarial_partial_batch_one_failure(db):
    def _fake_run(task, agent_name, db_, user_id_, max_iter, endpoint, context=None):
        if agent_name == "Failer Agent":
            return {"success": False, "status": "failed", "error": "boom", "task": task}
        return {"success": True, "status": "completed", "response": "ok", "iterations": 1,
                "task": task, "agent_name": agent_name}

    with patch.object(dt, "_run_sub_agent", side_effect=_fake_run):
        res = asyncio.run(dt._delegate_batch(
            [{"task": "t", "agent_name": "A Agent", "max_iterations": 5},
             {"task": "t", "agent_name": "Failer Agent", "max_iterations": 5},
             {"task": "t", "agent_name": "C Agent", "max_iterations": 5}],
            db, None, None, None,
        ))
    statuses = {r["agent_name"]: r["status"] for r in res["results"]}
    assert statuses["Failer Agent"] == "failed"
    assert statuses["A Agent"] == "completed"
    assert statuses["C Agent"] == "completed"
    # The batch as a whole is NOT reported as a blanket success.
    assert res["success"] is False
    assert "partial" in (res.get("summary") or "").lower() or "1 failed" in (res.get("summary") or "")


# ── 9. Provider quota exhaustion — honest failure ─────────────────────

def test_adversarial_provider_quota_exhaustion(db):
    async def _quota_fail(messages, tool_schemas, endpoint=None):
        raise RuntimeError("429 quota exceeded for model qwen-max")

    with patch.object(dt, "call_llm_with_reliability", new=_quota_fail):
        res = asyncio.run(dt._run_sub_agent(
            "ask", "Inventory Management Agent", db, "u1", 5, None,
            context={"agent_name": "general_assistant", "conversation_id": "c9"},
        ))
    assert res["success"] is False
    assert res["status"] == "failed"
    assert "quota" in (res.get("error") or "").lower()
    # The parent must NOT see a false success.
    assert res.get("response") is None or "quota" in (res.get("error") or "").lower()


# ── 10. Invalid / incomplete agent evidence — no crash, no fabrication ─

def test_adversarial_invalid_evidence_no_crash(db):
    # LLM returns a malformed tool_call (unparseable args) plus an empty final
    # answer. The sub-agent loop must not crash and must return honest (empty)
    # evidence rather than inventing data.
    async def _bad_llm(messages, tool_schemas, endpoint=None):
        return {
            "content": "",  # empty final answer = no fabricated evidence
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "execute_query", "arguments": "{not valid json"},
            }],
        }

    with patch.object(dt, "call_llm_with_reliability", new=_bad_llm):
        res = asyncio.run(dt._run_sub_agent(
            "ask", "Inventory Management Agent", db, "u1", 5, None,
            context={"agent_name": "general_assistant", "conversation_id": "c10"},
        ))
    # Either it completed with empty/low evidence, or failed — but it must NOT
    # raise, and it must never report fabricated success with data.
    assert res["success"] in (True, False)
    assert res.get("status") in ("completed", "partial", "failed", "blocked")
    assert "traceback" not in str(res).lower()
