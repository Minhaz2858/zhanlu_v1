"""Tests for the IBM multi-agent system fixes (2026-10-08).

1. Bounded delegate_task batch fan-out (was unbounded gather).
2. Parent-context inheritance for sub-agents.
3. Opt-in verification gate on sub-agent output.
4. LLM-queryable agent directory (list_available_agents).
5. Parallel ready-batch execution of the plan DAG.
"""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

from unittest.mock import patch

import pytest

from app.config import settings
from app.database import Base, engine, SessionLocal, get_db
import app.models  # noqa: F401
from app.models.agent_app import AgentApp
from app.models.execution import Execution, Plan, PlanNode, ObservationRecord
from app.services.tool_handlers import delegate_tool as dt


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


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(settings, "DELEGATE_VERIFY_ENABLED", False)
    monkeypatch.setattr(settings, "DELEGATE_MAX_CONCURRENT", 2)
    yield


# ── 1. bounded batch fan-out ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_fanout_is_bounded(db, monkeypatch):
    concurrent = {"now": 0, "max": 0}

    async def fake_sub(task, agent_name, db, user_id, max_iterations, endpoint=None, context=None):
        concurrent["now"] += 1
        concurrent["max"] = max(concurrent["max"], concurrent["now"])
        await asyncio.sleep(0.05)
        concurrent["now"] -= 1
        return {"success": True, "response": f"done: {task}", "iterations": 1}

    monkeypatch.setattr(dt, "_run_sub_agent", fake_sub)
    # P1 signature: _delegate_batch takes normalized {task, agent_name,
    # max_iterations} dicts (heterogeneous agents per item).
    result = await dt._delegate_batch(
        [{"task": f"task-{i}", "agent_name": "explore", "max_iterations": 5}
         for i in range(6)],
        db, "u1", None, None)
    assert result["count"] == 6
    assert result["success"] is True
    assert concurrent["max"] <= 2, "fan-out must respect DELEGATE_MAX_CONCURRENT"


# ── 2. context inheritance ──────────────────────────────────────────────

def test_parent_digest_from_conversation(db):
    from app.models.agent_conversation import AgentConversation

    conv = AgentConversation(
        agent_name="general_assistant",
        messages=[
            {"role": "user", "content": "Analyze Q3 sales for the ERP project"},
            {"role": "assistant", "content": "Q3 sales analysis started…"},
        ],
        org_id="default-org", app_id="default-app",
    )
    db.add(conv)
    db.commit()

    digest = dt._build_parent_context_digest(db, {"conversation_id": conv.id})
    assert "Q3 sales" in digest
    assert "user:" in digest and "assistant:" in digest


def test_parent_digest_from_context_bus():
    class _FakeBus:
        def format_for_react(self):
            return "BUS DIGEST CONTENT"

    digest = dt._build_parent_context_digest(None, {"context_bus": _FakeBus()})
    assert digest == "BUS DIGEST CONTENT"


def test_parent_digest_empty_without_context(db):
    assert dt._build_parent_context_digest(db, {}) == ""


@pytest.mark.asyncio
async def test_sub_agent_system_prompt_inherits_context(db, monkeypatch):
    captured = {}

    async def fake_llm(messages, tools, **kw):
        captured["system"] = messages[0]["content"]
        return {"content": "final answer", "tool_calls": []}

    monkeypatch.setattr(dt, "call_llm_with_reliability", fake_llm)
    monkeypatch.setattr(
        dt, "_build_parent_context_digest",
        lambda db, ctx: "PARENT GOAL: analyze Q3 sales")

    result = await dt._run_sub_agent_inner(
        "summarize the numbers", "explore", db, "u1", 3, None,
        context={"conversation_id": "c1"})
    assert result["success"] is True
    assert "PARENT GOAL: analyze Q3 sales" in captured["system"]


# ── 3. verification gate ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verification_disabled_by_default():
    assert await dt._verify_sub_agent_output("t", "good answer") is None


@pytest.mark.asyncio
async def test_verification_attaches_verdict_when_enabled(db, monkeypatch):
    monkeypatch.setattr(settings, "DELEGATE_VERIFY_ENABLED", True)

    class _R:
        verdict = "accept"
        completeness_score = 0.9
        confidence = 0.8

    with patch("app.services.synexia.quality_eval.evaluate_quality",
               return_value=_R()):
        v = await dt._verify_sub_agent_output("t", "good answer")
    assert v["verification"]["verdict"] == "accept"
    assert v["verification"]["completeness"] == 0.9


# ── 4. agent directory tool ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_available_agents_directory(db):
    from app.services.tool_registry import registry

    _seed_published(db, "dir-agent")
    entry = registry.get_entry("list_available_agents")
    assert entry is not None
    result = await entry.handler({}, db, "u1", None)
    assert result["success"] is True
    names = {a["name"] for a in result["agents"]}
    assert "explore" in names            # builtin definition
    assert "dir-agent" in names          # published user agent
    builtin = next(a for a in result["agents"] if a["name"] == "explore")
    assert builtin["kind"] == "builtin"
    user = next(a for a in result["agents"] if a["name"] == "dir-agent")
    assert user["kind"] == "user_agent" and user["agent_id"]


def _seed_published(db, name):
    agent = AgentApp(
        name=name, description="d", prompt_identity="x",
        status="published", org_id="default-org", app_id="default-app",
    )
    db.add(agent)
    db.commit()
    return agent


# ── 5. parallel plan-DAG ready batches ──────────────────────────────────

def _make_plan(db, deps_a=None, deps_b=None):
    execution = Execution(user_message="test", agent_name="general_assistant")
    db.add(execution)
    db.commit()
    plan = Plan(execution_id=execution.id, version=1, status="approved")
    db.add(plan)
    db.commit()
    node_a = PlanNode(plan_id=plan.id, seq=1, node_type="tool",
                      name="node_a", dependencies=deps_a or [],
                      inputs={"tool_name": "noop", "arguments": {}})
    node_b = PlanNode(plan_id=plan.id, seq=2, node_type="tool",
                      name="node_b", dependencies=deps_b or [],
                      inputs={"tool_name": "noop", "arguments": {}})
    node_c = PlanNode(plan_id=plan.id, seq=3, node_type="tool",
                      name="node_c", dependencies=[1],
                      inputs={"tool_name": "noop", "arguments": {}})
    db.add_all([node_a, node_b, node_c])
    db.commit()
    plan.nodes = [node_a, node_b, node_c]
    return execution, plan, [node_a, node_b, node_c]


@pytest.mark.asyncio
async def test_parallel_prepass_runs_independent_nodes_concurrently(db, monkeypatch):
    """Two independent nodes + one dependent — the pre-pass must run the
    independent pair concurrently (overlapped) and leave the dependent
    node for the serial loop."""
    from app.services.synexia import capability_router as cr

    execution, plan, nodes = _make_plan(db)
    timeline = {"start_a": None, "start_b": None, "end_a": None, "end_b": None}

    def fake_execute(db2, execution2, node2, user_id2, data_ctx_extras=None, endpoint=None):
        import time
        if node2.name == "node_a":
            timeline["start_a"] = time.monotonic()
            time.sleep(0.15)
            timeline["end_a"] = time.monotonic()
        elif node2.name == "node_b":
            timeline["start_b"] = time.monotonic()
            time.sleep(0.05)
            timeline["end_b"] = time.monotonic()
        node2.status = "completed"
        obs = ObservationRecord(
            execution_id=execution2.id, plan_node_id=node2.id, seq=0,
            observation_type="tool_call", tool_name=node2.name, success=True)
        db2.add(obs)
        db2.commit()
        return obs

    monkeypatch.setattr(cr, "_execute_single_node", fake_execute)
    observations: list = []
    cr._parallel_ready_batch_prepass(
        db, execution, plan, list(plan.nodes), "u1", None, None,
        observations, lambda *a, **k: None)

    # the independent pair overlapped (ran concurrently, not serially)
    assert timeline["start_a"] < timeline["end_b"] or timeline["start_b"] < timeline["end_a"]
    # the prepass drains the whole DAG: node_c's dep (node_a) completed in
    # round 1, so round 2 legitimately runs it → 3 observations
    assert len(observations) == 3
    db.expire_all()
    for name in ("node_a", "node_b", "node_c"):
        assert db.query(PlanNode).filter(
            PlanNode.plan_id == plan.id, PlanNode.name == name).first().status == "completed"


@pytest.mark.asyncio
async def test_parallel_prepass_skips_unsatisfiable_dependency(db):
    """A node whose dependency can never be satisfied (missing dep) must
    NOT be picked up by the pre-pass — it waits for the serial loop."""
    from app.services.synexia import capability_router as cr

    execution, plan, nodes = _make_plan(db, deps_b=[9])  # seq 9 doesn't exist
    ran = []

    def fake_execute(db2, execution2, node2, *a, **k):
        ran.append(node2.name)
        node2.status = "completed"
        obs = ObservationRecord(
            execution_id=execution2.id, plan_node_id=node2.id, seq=0,
            observation_type="tool_call", tool_name=node2.name, success=True)
        db2.add(obs)
        db2.commit()
        return obs

    with patch.object(cr, "_execute_single_node", fake_execute):
        cr._parallel_ready_batch_prepass(
            db, execution, plan, list(plan.nodes), "u1", None, None,
            [], lambda *a, **k: None)
    assert "node_b" not in ran
    assert "node_a" in ran  # the independent node still ran
    db.expire_all()
    # scope by plan_id — the shared test DB has node_b rows from other plans
    assert db.query(PlanNode).filter(
        PlanNode.plan_id == plan.id, PlanNode.name == "node_b").first().status == "pending"
