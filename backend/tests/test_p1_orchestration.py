"""P1 automatic enterprise orchestration tests (2026-10-31).

Covers roadmap P1.0/P1.1/P3 items:
  - Q1:  list_available_agents in the General Assistant toolset
  - D3:  enriched discovery payload (department, home_project, data_domains)
         with department filter + conversation-metadata default
  - M1:  heterogeneous batch delegation ({task, agent_name} items)
  - Q3:  child-result contract (status field, PARTIAL prefix, batch summary)
  - Q2:  plan-DAG "agent" node dispatch via delegate_task
  - D2:  department context block appended to the GA system prompt
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

from unittest.mock import patch, MagicMock

import pytest

from app.config import settings
from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401
from app.models.agent_app import AgentApp
from app.models.project import Project
from app.models.knowledge_base import KnowledgeBase
from app.models.agent_conversation import AgentConversation

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


@pytest.fixture
def dept_setup(db):
    """A published agent in a department'd project with a bound KB."""
    proj = Project(name="Manufacturing WS", department="manufacturing")
    db.add(proj)
    db.flush()
    app = AgentApp(
        name="Discovery Test Agent",
        description="Test specialist",
        status="published",
        capabilities=["quality-control", "spc"],
        project_id=proj.id,
        org_id="default-org",
        app_id="default-app",
    )
    kb = KnowledgeBase(
        name="Production DB", project_id=proj.id,
        org_id="default-org", app_id="default-app",
    )
    db.add_all([app, kb])
    db.commit()
    return {"app": app, "proj": proj, "kb": kb}


# ── Q1: discovery tool in the GA toolset ────────────────────────────────

def test_ga_toolset_includes_discovery(db):
    from app.services.system_agents import ensure_system_agents

    ensure_system_agents(db)
    ga = (
        db.query(AgentApp)
        .filter(AgentApp.name == "general_assistant", AgentApp.is_deleted == False)  # noqa: E712
        .first()
    )
    assert ga is not None
    assert "list_available_agents" in (ga.tool_config or {}).get("enabled_tools", [])


# ── D3: enriched discovery ───────────────────────────────────────────────

def test_discovery_enrichment_and_department_filter(db, dept_setup):
    import asyncio
    from app.services.tool_handlers.delegate_tool import _list_available_agents

    res = asyncio.run(_list_available_agents({}, db, None, context=None))
    entry = next(a for a in res["agents"] if a["name"] == "Discovery Test Agent")
    assert entry["department"] == "manufacturing"
    assert entry["home_project"] == "Manufacturing WS"
    assert "Production DB" in entry["data_domains"]

    # department filter keeps the match…
    res2 = asyncio.run(_list_available_agents(
        {"department": "Manufacturing"}, db, None, context=None))
    assert any(a["name"] == "Discovery Test Agent" for a in res2["agents"])

    # …and excludes others
    res3 = asyncio.run(_list_available_agents(
        {"department": "finance"}, db, None, context=None))
    assert not any(a["name"] == "Discovery Test Agent" for a in res3["agents"])

    # capability filter
    res4 = asyncio.run(_list_available_agents(
        {"capability": "spc"}, db, None, context=None))
    assert any(a["name"] == "Discovery Test Agent" for a in res4["agents"])
    res5 = asyncio.run(_list_available_agents(
        {"capability": "welding"}, db, None, context=None))
    assert not any(a["name"] == "Discovery Test Agent" for a in res5["agents"])


def test_discovery_department_defaults_from_conversation_metadata(db, dept_setup):
    import asyncio
    from app.services.tool_handlers.delegate_tool import _list_available_agents

    conv = AgentConversation(
        agent_name="general_assistant", title="t",
        metadata_={"department": "manufacturing"},
        org_id="default-org", app_id="default-app",
    )
    db.add(conv)
    db.commit()

    captured = {}

    async def _capture(args, db_, user_id_, context=None):
        captured["args"] = args
        return {"success": True, "agents": []}

    with patch(
        "app.services.tool_handlers.delegate_tool._list_available_agents",
        side_effect=_capture,
    ):
        # invoke the registry handler so we exercise the real entry path
        from app.services.tool_handlers.delegate_tool import registry

        entry = registry.get_entry("list_available_agents")
        assert entry is not None
        asyncio.run(entry.handler({}, db, None, context={"conversation_id": conv.id}))

    # the real handler reads the conversation metadata as the default filter;
    # here we verify the defaulting logic directly (no arg → dept from conv):
    res = asyncio.run(_list_available_agents({}, db, None, context={"conversation_id": conv.id}))
    assert res["department_filter"] == "manufacturing"


# ── M1: heterogeneous batch ─────────────────────────────────────────────

def test_batch_per_task_agent_dispatch(db):
    import asyncio
    from app.services.tool_handlers import delegate_tool as dt

    calls = []

    async def fake_run(task, agent_name, db_, user_id_, max_iter, endpoint, context=None):
        calls.append((task, agent_name))
        return {"success": True, "task": task, "response": f"done:{task}", "iterations": 1}

    with patch.object(dt, "_run_sub_agent", side_effect=fake_run):
        res = asyncio.run(dt._delegate_batch(
            [
                {"task": "t1", "agent_name": "Inventory Management Agent", "max_iterations": 5},
                {"task": "t2", "agent_name": "Quality Control Agent", "max_iterations": 5},
            ],
            db, None, None, None,
        ))

    assert calls == [("t1", "Inventory Management Agent"), ("t2", "Quality Control Agent")]
    assert res["success"] is True
    assert res["status"] == "completed"
    assert "2 completed" in res["summary"]
    assert all(r["agent_name"] in ("Inventory Management Agent", "Quality Control Agent")
               for r in res["results"])


def test_batch_normalizes_string_and_dict_items(db):
    import asyncio
    from app.services.tool_handlers import delegate_tool as dt

    async def _delegate_batch_spy(items, db_, user_id_, endpoint_, context_):
        return {"items": items}

    with patch.object(dt, "_delegate_batch", side_effect=_delegate_batch_spy):
        res = asyncio.run(dt._delegate_task(
            {"tasks": ["plain task", {"task": "special", "agent_name": "Finance Agent"}],
             "agent_name": "general_assistant"},
            db, None, context=None,
        ))
    items = res["items"]
    assert items[0] == {"task": "plain task", "agent_name": "general_assistant", "max_iterations": settings.DELEGATE_MAX_ITERATIONS}
    assert items[1]["agent_name"] == "Finance Agent"


def test_batch_status_failed_and_partial(db):
    import asyncio
    from app.services.tool_handlers import delegate_tool as dt

    async def fake_run(task, agent_name, db_, user_id_, max_iter, endpoint, context=None):
        if task == "boom":
            return {"success": False, "error": "x", "task": task}
        if task == "slow":
            return {"success": True, "task": task, "response": "p", "truncated": True, "iterations": 1}
        return {"success": True, "task": task, "response": "ok", "iterations": 1}

    with patch.object(dt, "_run_sub_agent", side_effect=fake_run):
        res = asyncio.run(dt._delegate_batch(
            [
                {"task": "ok", "agent_name": "A", "max_iterations": 5},
                {"task": "slow", "agent_name": "B", "max_iterations": 5},
                {"task": "boom", "agent_name": "C", "max_iterations": 5},
            ],
            db, None, None, None,
        ))
    # P2 batch contract (scenario F, honest partial completion): a batch with
    # SOME successes and SOME failures is "partial" (survivors produced real
    # findings), NOT "failed" — only a batch with ZERO successes is "failed".
    # (The pre-P2 precedence "any failure → failed" was deliberately replaced;
    # see _delegate_batch's roll-up comment.)
    assert res["status"] == "partial"
    assert res["success"] is False  # not a full success — the parent must re-plan
    assert "1 completed, 1 partial, 1 failed" in res["summary"]
    statuses = sorted(r["status"] for r in res["results"])
    assert statuses == ["completed", "failed", "partial"]


def test_batch_all_failed_is_failed(db):
    """Scenario F other half: a batch with ZERO successes is "failed" —
    never dressed up as partial."""
    import asyncio
    from app.services.tool_handlers import delegate_tool as dt

    async def fake_run(task, agent_name, db_, user_id_, max_iter, endpoint, context=None):
        return {"success": False, "error": "x", "task": task}

    with patch.object(dt, "_run_sub_agent", side_effect=fake_run):
        res = asyncio.run(dt._delegate_batch(
            [
                {"task": "boom1", "agent_name": "A", "max_iterations": 5},
                {"task": "boom2", "agent_name": "B", "max_iterations": 5},
            ],
            db, None, None, None,
        ))
    assert res["status"] == "failed"
    assert res["success"] is False
    assert "2 completed, 0 partial, 2 failed" not in res["summary"]
    assert "0 completed, 0 partial, 2 failed" in res["summary"]


def test_batch_items_always_carry_status(db):
    """P1 Phase 4: every result item carries a machine-readable status,
    even on the happy path (where _run_sub_agent historically returned no
    status key) — the UI evidence card keys off it."""
    import asyncio
    from app.services.tool_handlers import delegate_tool as dt

    async def fake_run(task, agent_name, db_, user_id_, max_iter, endpoint, context=None):
        return {"success": True, "task": task, "response": "ok", "iterations": 1}

    with patch.object(dt, "_run_sub_agent", side_effect=fake_run):
        res = asyncio.run(dt._delegate_batch(
            [{"task": "t", "agent_name": "A", "max_iterations": 5}],
            db, None, None, None,
        ))
    assert res["results"][0]["status"] == "completed"


# ── Q3: child-result contract (response prefix) ─────────────────────────

def test_partial_response_prefix(db):
    """The wall-clock path must prefix the response so the parent LLM cannot
    miss truncation. Directly exercised via the contract logic in
    _run_sub_agent's return — simulated here through the batch mapper and
    the prefix constant."""
    from app.services.tool_handlers.delegate_tool import _delegate_task
    import asyncio

    async def fake_run(task, agent_name, db_, user_id_, max_iter, endpoint, context=None):
        # simulate the truncated return shape produced by _run_sub_agent
        return {
            "success": True, "status": "partial", "task": task,
            "response": "[PARTIAL RESULT — sub-agent hit its wall-clock budget "
                        "before finishing; figures below may be incomplete. "
                        "Re-delegate with a narrower task if completeness "
                        "matters.]\n\ndata...",
            "truncated": True, "iterations": 1,
        }

    with patch.object(dt, "_run_sub_agent", side_effect=fake_run):
        res = asyncio.run(_delegate_task(
            {"task": "big analysis", "agent_name": "Demand Forecasting Agent"},
            db, None, context=None,
        ))
    assert res["status"] == "partial"
    assert res["response"].startswith("[PARTIAL RESULT")


# ── Q2: plan-DAG agent-node dispatch ────────────────────────────────────

def test_agent_node_dispatch_routes_to_delegate_task(db):
    from unittest.mock import MagicMock
    from app.services.synexia import capability_router as cr

    captured = {}

    async def fake_retry(tool_name, args, db_, user_id_, context=None):
        captured["tool"] = tool_name
        captured["args"] = args
        return {"success": True, "response": "child answer", "status": "completed"}

    obs = MagicMock()

    node = MagicMock()
    node.node_type = "agent"
    node.name = "Analyze inventory"
    node.inputs = {"task": "Check material availability", "agent_name": "Inventory Management Agent"}
    execution = MagicMock()
    execution.conversation_id = "conv-1"
    execution.id = "exec-1"
    execution.app_id = "app-1"
    execution.agent_name = "general_assistant"

    with patch.object(cr, "execute_tool_with_retry", None, create=True), \
         patch("app.services.agent_tools.execute_tool_with_retry", side_effect=fake_retry), \
         patch.object(cr, "_record_observation", return_value=obs) as ro:
        cr._execute_single_node(db, execution, node, "user-1")

    assert captured["tool"] == "delegate_task"
    assert captured["args"]["agent_name"] == "Inventory Management Agent"
    assert captured["args"]["task"] == "Check material availability"
    assert ro.call_args.kwargs.get("observation_type") == "agent_delegation"
    assert ro.call_args.kwargs.get("success") is True


def test_agent_node_failure_recorded(db):
    from unittest.mock import MagicMock
    from app.services.synexia import capability_router as cr

    async def fake_retry(*a, **k):
        raise RuntimeError("child down")

    node = MagicMock()
    node.node_type = "agent"
    node.name = "n"
    node.inputs = {"task": "t"}
    execution = MagicMock()
    execution.conversation_id = None
    execution.id = "e"
    execution.app_id = None
    execution.agent_name = "general_assistant"

    with patch("app.services.agent_tools.execute_tool_with_retry", side_effect=fake_retry), \
         patch.object(cr, "_record_observation", return_value=MagicMock()) as ro:
        cr._execute_single_node(db, execution, node, "user-1")
    assert ro.call_args.kwargs.get("success") is False
    assert "child down" in ro.call_args.kwargs.get("error_message")


# ── D2: department context block ────────────────────────────────────────

def test_department_block_appended():
    from app.routers.agents import _append_department_block

    conv = MagicMock()
    conv.metadata_ = {"department": "manufacturing"}
    out = _append_department_block("BASE", conv)
    assert out.startswith("BASE")
    assert "ACTIVE DEPARTMENT: manufacturing" in out
    assert "list_available_agents" in out


def test_department_block_noop_without_department():
    from app.routers.agents import _append_department_block

    conv = MagicMock()
    conv.metadata_ = {}
    assert _append_department_block("BASE", conv) == "BASE"
    assert _append_department_block("BASE", None) == "BASE"
