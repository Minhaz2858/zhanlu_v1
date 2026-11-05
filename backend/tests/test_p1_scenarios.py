"""P1 Phase 3 golden scenario harness (2026-10-31).

Roadmap M2: "Golden multi-agent scenarios as tests: production shortage,
quality investigation, monthly report — asserting ≥2 departments
coordinated, grounded figures, explicit partials."

These are NOT live-LLM tests (P0 provider-quota blocker is external).
The LLM boundary is mocked; everything AROUND it — the real DAG agent-node
dispatch, the real batch delegation aggregation, the real discovery
scoping, the real result contract — executes for real.

Scenarios:
  A  Production shortage  — a plan-DAG with agent nodes in two different
     departments (inventory + procurement) executes both, each routed to
     its own specialist through the governed delegate_task path.
  B  Quality investigation — a heterogeneous batch where one specialist
     hits its wall-clock budget: the batch surfaces status=partial, the
     summary counts are exact, and the truncated child's response carries
     the unavoidable [PARTIAL RESULT] prefix.
  C  Monthly report — department-scoped discovery: a conversation stamped
     with metadata_.department makes the GA's discovery default to that
     department's specialists, and a capability filter composes with it.
  D  Failure honesty — a child that raises records a FAILED observation
     (never a fake success), and an exception child surfaces as status=failed.
  E  Cross-department batch — one batch, one agent per department,
     per-item agent attribution preserved end-to-end.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

from unittest.mock import patch, MagicMock  # noqa: E402

import pytest  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
import app.models  # noqa: F401
from app.models.agent_app import AgentApp  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.models.agent_conversation import AgentConversation  # noqa: E402

try:  # registration side effects
    import app.services.tool_handlers.delegate_tool  # noqa: F401
except ImportError:  # pragma: no cover
    pass

from app.services.tool_handlers import delegate_tool as dt  # noqa: E402


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


# ── Scenario A: production shortage (cross-department plan-DAG) ─────────

def test_scenario_a_cross_department_dag(db):
    """GA plans two agent nodes in different departments; both dispatch to
    their own specialist via the governed delegate_task path and both
    record successful agent_delegation observations."""
    from app.services.synexia import capability_router as cr

    calls = []

    async def fake_retry(tool_name, args, db_, user_id_, context=None):
        calls.append((tool_name, args["agent_name"], args["task"]))
        return {"success": True, "status": "completed",
                "response": f"grounded answer for {args['agent_name']}"}

    node_inv = MagicMock()
    node_inv.node_type = "agent"
    node_inv.name = "Inventory check"
    node_inv.inputs = {"task": "Which SKU lines fall below safety stock?",
                       "agent_name": "Inventory Management Agent"}
    node_proc = MagicMock()
    node_proc.node_type = "agent"
    node_proc.name = "Procurement check"
    node_proc.inputs = {"objective": "Which open POs cover the shortage?",
                        "agent_name": "Procurement Agent"}
    execution = MagicMock()
    execution.conversation_id = "conv-A"
    execution.id = "exec-A"
    execution.app_id = "app-A"
    execution.agent_name = "general_assistant"

    observed = []
    with patch("app.services.agent_tools.execute_tool_with_retry", side_effect=fake_retry), \
         patch.object(cr, "_record_observation",
                      side_effect=lambda *a, **kw: observed.append(kw) or MagicMock()):
        cr._execute_single_node(db, execution, node_inv, "user-A")
        cr._execute_single_node(db, execution, node_proc, "user-A")

    # Both nodes delegated — and NOT to the same agent (heterogeneous).
    assert [c[0] for c in calls] == ["delegate_task", "delegate_task"]
    assert calls[0][1] == "Inventory Management Agent"
    assert calls[1][1] == "Procurement Agent"
    # objective falls back to the task brief; node.name is the last resort.
    assert calls[1][2] == "Which open POs cover the shortage?"
    assert all(o["observation_type"] == "agent_delegation" and o["success"] for o in observed)


# ── Scenario B: quality investigation (batch with an explicit partial) ──

def test_scenario_b_batch_surfaces_partial(db):
    """One specialist truncates mid-investigation: batch status=partial,
    exact summary counts, and the child response carries the PARTIAL
    prefix so the parent LLM cannot silently merge it."""
    partial_prefix = ("[PARTIAL RESULT — sub-agent hit its wall-clock budget "
                      "before finishing; figures below may be incomplete. "
                      "Re-delegate with a narrower task if completeness "
                      "matters.]\n\n")

    async def fake_run(task, agent_name, db_, user_id_, max_iter, endpoint, context=None):
        if agent_name == "Quality Control Agent":
            return {"success": True, "status": "partial", "task": task,
                    "agent_name": agent_name, "truncated": True,
                    "response": partial_prefix + "defect trend: 3 lots open…",
                    "iterations": max_iter}
        return {"success": True, "status": "completed", "task": task,
                "agent_name": agent_name, "response": f"done: {task}",
                "iterations": 2}

    with patch.object(dt, "_run_sub_agent", side_effect=fake_run):
        res = asyncio_run(dt._delegate_batch(
            [
                {"task": "SPC trend on line 3", "agent_name": "Quality Control Agent", "max_iterations": 4},
                {"task": "maintenance history of station 7", "agent_name": "Equipment Maintenance Agent", "max_iterations": 4},
            ],
            db, None, None, None,
        ))

    assert res["status"] == "partial"
    assert res["summary"] == "1 completed, 1 partial, 0 failed"
    qc = next(r for r in res["results"] if r["agent_name"] == "Quality Control Agent")
    assert qc["status"] == "partial" and qc["truncated"] is True
    assert qc["response"].startswith("[PARTIAL RESULT")


# ── Scenario C: monthly report (department-scoped discovery) ────────────

def test_scenario_c_department_scoped_discovery(db):
    """A conversation stamped with department makes discovery default to
    that department's published specialists; a capability filter composes
    on top; other departments' agents stay out of the default view."""
    inv_proj = Project(name="Inventory WS", department="inventory")
    fin_proj = Project(name="Finance WS", department="finance")
    db.add_all([inv_proj, fin_proj])
    db.flush()
    inv_agent = AgentApp(name="ScenarioC Inventory Agent", description="stock",
                         status="published", project_id=inv_proj.id,
                         org_id="default-org", app_id="default-app")
    fin_agent = AgentApp(name="ScenarioC Finance Agent", description="P&L",
                         status="published", capabilities=["monthly-report", "pnl"],
                         project_id=fin_proj.id,
                         org_id="default-org", app_id="default-app")
    inv_kb = KnowledgeBase(name="Stock Ledger", project_id=inv_proj.id,
                           org_id="default-org", app_id="default-app")
    db.add_all([inv_agent, fin_agent, inv_kb])
    conv = AgentConversation(agent_name="general_assistant", title="t",
                             metadata_={"department": "inventory"},
                             org_id="default-org", app_id="default-app")
    db.add_all([inv_agent, fin_agent, inv_kb, conv])
    db.commit()

    res = asyncio_run(dt._list_available_agents({}, db, None,
                                                context={"conversation_id": conv.id}))
    names = [a["name"] for a in res["agents"]]
    assert "ScenarioC Inventory Agent" in names
    assert "ScenarioC Finance Agent" not in names  # department default filtered
    assert res["department_filter"] == "inventory"
    inv_entry = next(a for a in res["agents"] if a["name"] == "ScenarioC Inventory Agent")
    assert inv_entry["department"] == "inventory"
    assert inv_entry["home_project"] == "Inventory WS"
    assert "Stock Ledger" in inv_entry["data_domains"]

    # Capability filter composes as AND with the department default —
    # cross-department expansion is EXPLICIT: the GA passes department
    # when the default scope lacks the needed capability (prompt rule:
    # "expand only when capabilities are absent").
    res2 = asyncio_run(dt._list_available_agents(
        {"capability": "monthly-report", "department": "finance"}, db, None,
        context={"conversation_id": conv.id}))
    names2 = [a["name"] for a in res2["agents"]]
    assert "ScenarioC Finance Agent" in names2
    assert res2["department_filter"] == "finance"


# ── Scenario D: failure honesty ─────────────────────────────────────────

def test_scenario_d_failures_are_explicit(db):
    """A raising child and an error-returning child both surface as
    status=failed — never a fake success — and the batch status degrades
    to failed."""
    from app.services.synexia import capability_router as cr

    # D1: DAG agent node whose delegation raises → FAILED observation.
    async def boom(*a, **k):
        raise RuntimeError("child down")

    node = MagicMock()
    node.node_type = "agent"
    node.name = "Broken delegation"
    node.inputs = {"task": "t", "agent_name": "Whatever Agent"}
    execution = MagicMock()
    execution.conversation_id = "conv-D"
    execution.id = "exec-D"
    execution.app_id = "app-D"
    execution.agent_name = "general_assistant"
    with patch("app.services.agent_tools.execute_tool_with_retry", side_effect=boom), \
         patch.object(cr, "_record_observation", return_value=MagicMock()) as ro:
        cr._execute_single_node(db, execution, node, "user-D")
    assert ro.call_args.kwargs.get("success") is False
    assert "child down" in ro.call_args.kwargs.get("error_message")

    # D2: batch child returning an error dict → status=failed.
    async def err_run(task, agent_name, db_, user_id_, max_iter, endpoint, context=None):
        if task == "explode":
            return {"success": False, "error": "LLM quota exhausted", "task": task}
        return {"success": True, "task": task, "response": "ok", "iterations": 1}

    with patch.object(dt, "_run_sub_agent", side_effect=err_run):
        res = asyncio_run(dt._delegate_batch(
            [{"task": "explode", "agent_name": "A", "max_iterations": 3},
             {"task": "fine", "agent_name": "B", "max_iterations": 3}],
            db, None, None, None))
    # P2 (scenario F): a batch that produced SOME successful results but also had
    # a failure is "partial" (you got real findings from the survivor), not
    # "failed". Only a batch with ZERO successes is "failed".
    assert res["status"] == "partial"
    assert res["success"] is False
    bad = next(r for r in res["results"] if r["task"] == "explode")
    assert bad["status"] == "failed"
    good = next(r for r in res["results"] if r["task"] == "fine")
    assert good["status"] == "completed"


# ── Scenario E: one batch, one agent per department ─────────────────────

def test_scenario_e_cross_department_batch(db):
    """Heterogeneous batch: every child is attributed to its own agent and
    the emitted activity labels carry agent names (what the UI renders)."""
    agents = ["Inventory Management Agent", "Procurement Agent",
              "Quality Control Agent"]
    seen = []

    async def fake_run(task, agent_name, db_, user_id_, max_iter, endpoint, context=None):
        seen.append(agent_name)
        return {"success": True, "status": "completed", "task": task,
                "agent_name": agent_name, "response": f"ok: {task}", "iterations": 1}

    tasks = [{"task": f"step {i}", "agent_name": a, "max_iterations": 3}
             for i, a in enumerate(agents)]
    with patch.object(dt, "_run_sub_agent", side_effect=fake_run):
        res = asyncio_run(dt._delegate_batch(tasks, db, None, None, None))

    assert seen == agents
    assert res["count"] == 3
    assert [r["agent_name"] for r in res["results"]] == agents
    assert res["status"] == "completed"
    assert "3 completed" in res["summary"]


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)
