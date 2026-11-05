"""P2 Phase 2 — Intelligent Agent Collaboration (2026-10-31).

LLM boundary is mocked; everything AROUND it — the Collaboration Broker
governance, the shared-evidence registry, the reviewer service, the delegate_task
review integration, and the DAG reviewer/conditional/approval extensions — runs
for real. These are NOT live-LLM tests (provider quota is externally blocked).

Coverage (maps to the acceptance scenarios):
  A  Manufacturing root-cause (Quality + Maintenance + Production) via plan-DAG,
     partial completion when one specialist fails.
  B  Supply Chain requests Inventory Agent assistance (Pattern C) + evidence
     shared by reference.
  C  Reviewer detects an unsupported numerical claim.
  D  Reviewer requests additional evidence -> targeted re-investigation.
  E  Finance + Sales collaborate on vehicle profitability (evidence + review).
  F  One specialist fails while others return partial findings (batch honesty).
  G  Duplicate + circular collaboration attempts are blocked.

Plus unit tests for shared_evidence, reviewer fact-safety, and conditional
peer-collaboration tool exposure.
"""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

from unittest.mock import patch, MagicMock  # noqa: E402

import pytest  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
import app.models  # noqa: F401
from app.models.agent_app import AgentApp  # noqa: E402

from app.services.tool_handlers import delegate_tool as dt  # noqa: E402
from app.services.agent_collaboration import broker  # noqa: E402
from app.services.agent_collaboration import shared_evidence as se  # noqa: E402
from app.services.agent_collaboration import reviewer as rv  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _reset():
    se.reset_registry()
    broker.reset_concurrency()
    broker.reset_conv_state()
    from app.services.agent_collaboration.governance_store import reset_store
    reset_store()
    yield
    se.reset_registry()
    broker.reset_concurrency()
    broker.reset_conv_state()
    reset_store()


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
        AgentApp(name="Quality Control Agent", description="qc", status="published",
                 org_id="default-org", app_id="default-app"),
        AgentApp(name="Equipment Maintenance Agent", description="maint", status="published",
                 org_id="default-org", app_id="default-app"),
        AgentApp(name="Production Scheduling Agent", description="prod", status="published",
                 org_id="default-org", app_id="default-app"),
        AgentApp(name="Supply Chain Agent", description="sc", status="published",
                 org_id="default-org", app_id="default-app"),
        AgentApp(name="Inventory Management Agent", description="inv", status="published",
                 org_id="default-org", app_id="default-app"),
        AgentApp(name="Finance Agent", description="fin", status="published",
                 org_id="default-org", app_id="default-app"),
        AgentApp(name="Sales Agent", description="sales", status="published",
                 org_id="default-org", app_id="default-app"),
    ])
    db.commit()


# ── Unit: shared evidence registry (Part 3) ──────────────────────────────

def test_evidence_store_get_list_find(db):
    eid = se.provide_evidence(
        "conv-1", "Inventory Management Agent",
        source_id="ds-1", findings="safety stock = 120 units",
        assumptions=["lead time 5d"], validation_status=se.VALIDATION_VALIDATED,
    )
    item = se.get_evidence("conv-1", eid)
    assert item is not None
    assert item.findings == "safety stock = 120 units"
    assert item.validation_status == se.VALIDATION_VALIDATED
    refs = se.request_evidence_references("conv-1", "safety stock")
    assert len(refs) == 1
    assert refs[0]["evidence_id"] == eid
    # find_by_topic is a loose match; unrelated topic returns nothing
    assert se.request_evidence_references("conv-1", "zzz-nope") == []


def test_evidence_isolation_per_conversation(db):
    se.provide_evidence("cA", "AgentA", findings="only in A")
    assert se.request_evidence_references("cB", "only in A") == []


def test_evidence_request_returns_reference_not_raw(db):
    eid = se.provide_evidence(
        "conv-2", "Sales Agent", source_id="ds-2",
        findings={"units": 5000, "revenue": 1000000},
    )
    refs = se.request_evidence_references("conv-2", "units")
    # The reference does NOT carry the raw findings object — only a summary.
    assert refs[0]["evidence_id"] == eid
    assert "findings_summary" in refs[0]
    assert "findings" not in refs[0]


# ── Unit: reviewer fact-safety (Part 1) ─────────────────────────────────

def _patch_reviewer(payload):
    return patch.object(rv, "chat_completion_json_sync", return_value=payload)


def test_reviewer_returns_structured_verdict():
    payload = {
        "dimensions": {d: {"status": "ok", "detail": "fine"} for d in rv.DIMENSIONS},
        "overall": "approved",
        "answered": True,
        "issues": [],
        "needs_evidence": [],
    }
    with _patch_reviewer(payload):
        rep = rv.review_findings("q", "answer", evidence_items=[])
    assert rep.overall == "approved"
    assert rep.answered is True
    assert all(rep.dimensions[d]["status"] == "ok" for d in rv.DIMENSIONS)


def test_reviewer_detects_unsupported_claim():
    payload = {
        "dimensions": {
            "numerical_accuracy": {"status": "ok", "detail": ""},
            "evidence_completeness": {"status": "ok", "detail": ""},
            "source_attribution": {"status": "warn", "detail": "no source"},
            "contradictory_findings": {"status": "fail", "detail": "conflict"},
            "unsupported_conclusions": {"status": "fail", "detail": "claimed X w/o evidence"},
            "assumptions_uncertainty": {"status": "warn", "detail": ""},
            "question_answered": {"status": "ok", "detail": ""},
        },
        "overall": "needs_revision",
        "answered": True,
        "issues": ["unsupported claim X"],
        "needs_evidence": ["measure X from source"],
    }
    with _patch_reviewer(payload):
        rep = rv.review_findings("q", "answer", evidence_items=[])
    assert rep.overall == "needs_revision"
    assert rep.dimensions["unsupported_conclusions"]["status"] == "fail"
    assert "measure X from source" in rep.needs_evidence


def test_reviewer_never_invents_on_parse_failure():
    # If the judge is unavailable, reviewer returns a conservative (non-approved)
    # report — it never approves fake content.
    with _patch_reviewer({}):
        rep = rv.review_findings("q", "answer", evidence_items=[])
    assert rep.overall == "needs_revision"
    assert rep.dimensions["numerical_accuracy"]["status"] == "warn"


# ── Unit: conditional peer collaboration tool exposure (Part 2) ──────────

def test_child_toolset_includes_evidence_and_delegate_when_nested_enabled(db):
    captured = {}

    def fake_llm(messages, tool_schemas, endpoint=None):
        captured["tools"] = tool_schemas
        return {"content": "done", "tool_calls": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    with patch.object(settings := dt.settings, "COLLAB_NESTED_DELEGATION_ENABLED", True), \
         patch.object(dt, "call_llm_with_reliability", side_effect=fake_llm):
        asyncio.run(dt._run_sub_agent_inner(
            "task", "general_assistant", db, None, 3, None,
            context={"conversation_id": "c-ts", "agent_name": "general_assistant"},
            wall_seconds=30,
        ))
    names = [t.get("function", {}).get("name") for t in captured["tools"]]
    assert "provide_evidence" in names
    assert "request_evidence" in names
    assert "delegate_task" in names  # Pattern C enabled


def test_child_toolset_excludes_delegate_when_nested_disabled(db):
    captured = {}

    def fake_llm(messages, tool_schemas, endpoint=None):
        captured["tools"] = tool_schemas
        return {"content": "done", "tool_calls": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    with patch.object(dt.settings, "COLLAB_NESTED_DELEGATION_ENABLED", False), \
         patch.object(dt, "call_llm_with_reliability", side_effect=fake_llm):
        asyncio.run(dt._run_sub_agent_inner(
            "task", "general_assistant", db, None, 3, None,
            context={"conversation_id": "c-ts2", "agent_name": "general_assistant"},
            wall_seconds=30,
        ))
    names = [t.get("function", {}).get("name") for t in captured["tools"]]
    assert "provide_evidence" in names  # evidence always available
    assert "delegate_task" not in names  # nested delegation disabled


# ── Integration: reviewer wired into delegate_task(review=True) ──────────

def test_delegate_review_flag_attaches_report(db):
    good = {
        "dimensions": {d: {"status": "ok", "detail": ""} for d in rv.DIMENSIONS},
        "overall": "approved", "answered": True, "issues": [], "needs_evidence": [],
    }
    async def fake_inner(task, agent_name, db_, user_id_, max_iter, endpoint, context=None, agent_app=None, wall_seconds=None):
        return {"success": True, "status": "completed", "task": task,
                "agent_name": agent_name, "response": "final answer", "iterations": 1}

    with _patch_reviewer(good), patch.object(dt, "_run_sub_agent_inner", side_effect=fake_inner):
        res = asyncio.run(dt._delegate_task(
            {"task": "analyze failures", "agent_name": "Quality Control Agent", "review": True},
            db, None, context={"conversation_id": "c-rev", "agent_name": "general_assistant"},
        ))
    assert res["success"] is True
    assert res["review"]["overall"] == "approved"
    assert res["review"]["answered"] is True


def test_review_triggers_targeted_reinvestigation(db):
    # First review reports missing evidence -> one targeted follow-up delegation.
    gaps = {
        "dimensions": {d: {"status": "warn", "detail": ""} for d in rv.DIMENSIONS},
        "overall": "needs_revision", "answered": False,
        "issues": ["missing X"], "needs_evidence": ["measure defect rate from source"],
    }
    calls = []

    async def fake_inner(task, agent_name, db_, user_id_, max_iter, endpoint, context=None, agent_app=None, wall_seconds=None):
        calls.append(task)
        return {"success": True, "status": "completed", "task": task,
                "agent_name": agent_name, "response": f"answer for {task}", "iterations": 1}

    with _patch_reviewer(gaps), patch.object(dt, "_run_sub_agent_inner", side_effect=fake_inner):
        res = asyncio.run(dt._delegate_task(
            {"task": "root cause", "agent_name": "Quality Control Agent", "review": True},
            db, None, context={"conversation_id": "c-rev2", "agent_name": "general_assistant"},
        ))
    # Original + exactly one bounded follow-up (COLLAB_REVIEW_MAX_RETRIES=1).
    assert len(calls) == 2
    assert "root cause" in calls[0]
    assert "measure defect rate" in calls[1]
    assert res["review"]["overall"] == "needs_revision"


# ── Scenario G: duplicate + circular blocked (governance) ────────────────

def test_duplicate_delegation_blocked(db):
    ctx = {"conversation_id": "c-g", "agent_name": "general_assistant"}
    ok1, _, _ = broker.evaluate_delegation(
        target_agent_id="Inventory Management Agent", objective="count stock",
        context=ctx, db=db)
    assert ok1 is True
    # Same requester+target+objective again -> duplicate blocked.
    ok2, reason2, _ = broker.evaluate_delegation(
        target_agent_id="Inventory Management Agent", objective="count stock",
        context=ctx, db=db)
    assert ok2 is False
    assert "duplicate" in (reason2 or "").lower()


def test_circular_delegation_blocked(db):
    ctx = {"conversation_id": "c-g2", "agent_name": "general_assistant",
           "delegation_path": ["Supply Chain Agent"]}
    ok, reason, _ = broker.evaluate_delegation(
        target_agent_id="Supply Chain Agent", objective="loop", context=ctx, db=db)
    assert ok is False
    assert "cycle" in (reason or "").lower()


def test_nested_peer_collaboration_allowed(db):
    # Supply Chain (already on path) may ask Inventory for help — no cycle.
    ctx = {"conversation_id": "c-g3", "agent_name": "general_assistant",
           "delegation_path": ["Supply Chain Agent"]}
    ok, _, ext = broker.evaluate_delegation(
        target_agent_id="Inventory Management Agent", objective="verify shortage",
        context=ctx, db=db)
    assert ok is True
    # The child context now carries the extended path -> its own asks are governed.
    assert "Inventory Management Agent" in ext["delegation_path"]


# ── Scenario B: Supply Chain requests Inventory help + evidence by ref ──

def test_evidence_shared_between_specialists(db):
    # Inventory produces a finding; Supply Chain later requests it by reference.
    se.provide_evidence("c-b", "Inventory Management Agent", source_id="ds-inv",
                        findings="shortage of SKU-9: 40 units below safety",
                        validation_status=se.VALIDATION_VALIDATED)
    refs = se.request_evidence_references("c-b", "SKU-9")
    assert len(refs) == 1
    assert refs[0]["producer_agent"] == "Inventory Management Agent"
    assert refs[0]["source_id"] == "ds-inv"
    # The reference is enough to cite — Supply Chain never re-queries the rows.
    assert "findings" not in refs[0]


# ── Scenarios A / E / F via plan-DAG ────────────────────────────────────

def _node(name, node_type, inputs=None, run_if=None):
    n = MagicMock()
    n.node_type = node_type
    n.name = name
    n.inputs = dict(inputs or {})
    if run_if is not None:
        n.inputs["run_if"] = run_if
    n.status = "pending"
    n.dependencies = []
    return n


def _mock_agent_responses():
    """Side effect for execute_tool_with_retry keyed by agent_name."""
    def _fake(tool_name, args, db_, user_id_, context=None):
        an = args.get("agent_name")
        if an == "Equipment Maintenance Agent":
            return {"success": False, "status": "failed", "error": "tool timeout",
                    "response": "", "agent_name": an}
        return {"success": True, "status": "completed",
                "response": f"findings from {an}", "agent_name": an,
                "evidence_ids": []}
    return _fake


def test_scenario_a_root_cause_partial_completion(db, agents):
    from app.services.synexia import capability_router as cr

    observed = []
    nodes = [
        _node("Quality check", "agent", {"task": "defect root cause", "agent_name": "Quality Control Agent"}),
        _node("Maintenance check", "agent", {"task": "station history", "agent_name": "Equipment Maintenance Agent"}),
        _node("Scheduling check", "agent", {"task": "line impact", "agent_name": "Production Scheduling Agent"}),
    ]
    execution = MagicMock()
    execution.conversation_id = "conv-A"
    execution.id = "exec-A"
    execution.app_id = "app-A"
    execution.agent_name = "general_assistant"

    with patch.object(settings := __import__("app.config", fromlist=["settings"]).settings, "PLAN_DAG_PARALLEL_ENABLED", False), \
         patch("app.services.agent_tools.execute_tool_with_retry", side_effect=_mock_agent_responses()), \
         patch.object(cr, "_record_observation", side_effect=lambda *a, **kw: observed.append(kw) or MagicMock()):
        cr._execute_single_node(db, execution, nodes[0], "user-A")
        cr._execute_single_node(db, execution, nodes[1], "user-A")
        cr._execute_single_node(db, execution, nodes[2], "user-A")

    # 2 completed, 1 failed — partial completion, no crash, no fake success.
    statuses = {o["request_args"]["agent_name"]: o["success"] for o in observed}
    assert statuses["Quality Control Agent"] is True
    assert statuses["Equipment Maintenance Agent"] is False
    assert statuses["Production Scheduling Agent"] is True


def test_scenario_f_batch_partial_honesty(db, agents):
    # One specialist fails; the batch surfaces the mix honestly.
    async def fake_run(task, agent_name, db_, user_id_, max_iter, endpoint, context=None):
        if agent_name == "Finance Agent":
            return {"success": False, "status": "failed", "task": task,
                    "agent_name": agent_name, "error": "boom", "iterations": 1}
        return {"success": True, "status": "completed", "task": task,
                "agent_name": agent_name, "response": f"ok {task}", "iterations": 2}

    with patch.object(dt, "_run_sub_agent", side_effect=fake_run):
        res = asyncio.run(dt._delegate_batch(
            [
                {"task": "margin", "agent_name": "Finance Agent", "max_iterations": 3},
                {"task": "volume", "agent_name": "Sales Agent", "max_iterations": 3},
            ],
            db, None, None, None,
        ))
    assert res["status"] == "partial"  # failed + completed
    by_agent = {r["agent_name"]: r["status"] for r in res["results"]}
    assert by_agent["Finance Agent"] == "failed"
    assert by_agent["Sales Agent"] == "completed"


def test_scenario_e_finance_sales_collaboration(db, agents):
    from app.services.synexia import capability_router as cr

    # Both specialists produce shared evidence first (by reference).
    se.provide_evidence("conv-E", "Finance Agent", source_id="ds-fin",
                        findings="vehicle margin = 18%", validation_status=se.VALIDATION_VALIDATED)
    se.provide_evidence("conv-E", "Sales Agent", source_id="ds-sales",
                        findings="units sold = 9000", validation_status=se.VALIDATION_VALIDATED)

    observed = []
    nodes = [
        _node("Finance", "agent", {"task": "margin", "agent_name": "Finance Agent"}),
        _node("Sales", "agent", {"task": "volume", "agent_name": "Sales Agent"}),
        _node("Reviewer", "reviewer", {"question": "vehicle profitability?",
                                       "content": "margin 18%, 9000 units",
                                       "agent_name": "Finance Agent"}),
    ]
    execution = MagicMock()
    execution.conversation_id = "conv-E"
    execution.id = "exec-E"
    execution.app_id = "app-E"
    execution.agent_name = "general_assistant"

    approved = {
        "dimensions": {d: {"status": "ok", "detail": ""} for d in rv.DIMENSIONS},
        "overall": "approved", "answered": True, "issues": [], "needs_evidence": [],
    }
    with patch.object(__import__("app.config", fromlist=["settings"]).settings, "PLAN_DAG_PARALLEL_ENABLED", False), \
         patch("app.services.agent_tools.execute_tool_with_retry", side_effect=_mock_agent_responses()), \
         patch.object(rv, "chat_completion_json_sync", return_value=approved), \
         patch.object(cr, "_record_observation", side_effect=lambda *a, **kw: observed.append(kw) or MagicMock()):
        for n in nodes:
            cr._execute_single_node(db, execution, n, "user-E")

    reviewer_obs = [o for o in observed if o.get("observation_type") == "reviewer"]
    assert reviewer_obs, "reviewer node must record an observation"
    assert reviewer_obs[0]["result_data"]["review"]["overall"] == "approved"


def test_scenario_c_reviewer_flags_unsupported_claim(db, agents):
    bad = {
        "dimensions": {d: {"status": "ok", "detail": ""} for d in rv.DIMENSIONS}
    }
    bad["dimensions"]["unsupported_conclusions"] = {"status": "fail", "detail": "fabricated ROI"}
    bad.update({"overall": "needs_revision", "answered": True,
                "issues": ["unsupported ROI figure"], "needs_evidence": []})
    async def fake_inner(task, agent_name, db_, user_id_, max_iter, endpoint, context=None, agent_app=None, wall_seconds=None):
        return {"success": True, "status": "completed", "task": task,
                "agent_name": agent_name, "response": "ROI is 300%", "iterations": 1}
    with _patch_reviewer(bad), patch.object(dt, "_run_sub_agent_inner", side_effect=fake_inner):
        res = asyncio.run(dt._delegate_task(
            {"task": "report ROI", "agent_name": "Finance Agent", "review": True},
            db, None, context={"conversation_id": "c-c", "agent_name": "general_assistant"},
        ))
    assert res["review"]["overall"] == "needs_revision"
    assert res["review"]["dimensions"]["unsupported_conclusions"]["status"] == "fail"


def test_scenario_d_reviewer_requests_evidence_dag(db, agents):
    from app.services.synexia import capability_router as cr

    observed = []
    nodes = [
        _node("Supply", "agent", {"task": "lead time", "agent_name": "Supply Chain Agent"}),
        _node("Reviewer", "reviewer", {"question": "supplier delay?",
                                       "content": "possible delay", "agent_name": "Supply Chain Agent"}),
    ]
    execution = MagicMock()
    execution.conversation_id = "conv-D"
    execution.id = "exec-D"
    execution.app_id = "app-D"
    execution.agent_name = "general_assistant"

    needs = {
        "dimensions": {d: {"status": "warn", "detail": ""} for d in rv.DIMENSIONS},
        "overall": "needs_revision", "answered": False,
        "issues": ["no stock data"], "needs_evidence": ["verify stock from Inventory"],
    }
    followups = {"task": None}

    def _fake_retry(tool_name, args, db_, user_id_, context=None):
        # The reviewer node's BOUNDED targeted follow-up carries a task that
        # names the gap ("Targeted follow-up ... verify stock ..."). The supply
        # node's own delegation does not.
        if "Targeted follow-up" in (args.get("task") or ""):
            followups["task"] = args.get("task")
            return {"success": True, "status": "completed", "response": "re-checked stock",
                    "agent_name": args.get("agent_name")}
        return {"success": True, "status": "completed", "response": "lead time 5d",
                "agent_name": args.get("agent_name")}

    with patch.object(__import__("app.config", fromlist=["settings"]).settings, "PLAN_DAG_PARALLEL_ENABLED", False), \
         patch("app.services.agent_tools.execute_tool_with_retry", side_effect=_fake_retry), \
         patch.object(rv, "chat_completion_json_sync", return_value=needs), \
         patch.object(cr, "_record_observation", side_effect=lambda *a, **kw: observed.append(kw) or MagicMock()):
        for n in nodes:
            cr._execute_single_node(db, execution, n, "user-D")

    # The reviewer node triggered a targeted follow-up delegation.
    assert followups["task"] is not None
    assert "verify stock" in followups["task"]


def test_node_condition_met_logic(db, agents):
    from app.services.synexia import capability_router as cr

    node = _node("Dependent", "agent",
                 {"task": "act", "agent_name": "Inventory Management Agent"},
                 run_if={"requires_evidence_topic": "SKU-9"})
    exec_no = MagicMock(); exec_no.conversation_id = "c-cond-no"
    exec_yes = MagicMock(); exec_yes.conversation_id = "c-cond-yes"
    se.provide_evidence("c-cond-yes", "Inventory Management Agent",
                        source_id="ds", findings="SKU-9 shortage 40 units")
    assert cr._node_condition_met(node, MagicMock(nodes=[]), exec_no) is False
    assert cr._node_condition_met(node, MagicMock(nodes=[]), exec_yes) is True


def test_conditional_node_skipped_in_plan(db, agents):
    from app.services.synexia import capability_router as cr
    from app.models.execution import Plan, PlanNode

    cond = PlanNode(
        id="pn-cond", seq=0, node_type="agent", name="Conditional",
        description="", dependencies=[], status="pending",
        inputs={"task": "act", "agent_name": "Inventory Management Agent",
                "run_if": {"requires_evidence_topic": "SKU-9"}},
    )
    plan = Plan(id="plan-cond", execution_id="exec-cond", version=1, status="draft",
                summary="t", is_acyclic=True, nodes=[cond])

    execution = MagicMock()
    execution.conversation_id = "c-cond-plan"
    execution.id = "exec-cond"
    execution.app_id = "app-cond"
    execution.agent_name = "general_assistant"
    execution.policy_decision = {}
    execution.user_message = "x"
    execution.task_spec = {}

    agent_calls = []
    async def _fake_inner(task, agent_name, db_, user_id_, max_iter, endpoint, context=None, agent_app=None, wall_seconds=None):
        agent_calls.append(agent_name)
        return {"success": True, "status": "completed", "task": task,
                "agent_name": agent_name, "response": "done", "iterations": 1}

    with patch.object(__import__("app.config", fromlist=["settings"]).settings, "PLAN_DAG_PARALLEL_ENABLED", False), \
         patch.object(dt, "_run_sub_agent_inner", side_effect=_fake_inner), \
         patch.object(cr, "_record_observation", side_effect=lambda *a, **kw: MagicMock()):
        cr.execute_plan_nodes(db, execution, plan, "user-cond")

    # The conditional node never executed as an agent delegation: skipped before
    # the agent call because no peer evidence matched the topic.
    assert cond.status == "skipped"
    assert "Inventory Management Agent" not in agent_calls


def _obs(kw):
    """Build a stand-in observation record whose .result_data is the REAL dict
    (so the approval-gate isinstance/lookup logic is exercised), like the real
    _record_observation would return."""
    m = MagicMock()
    m.result_data = kw.get("result_data")
    m.observation_type = kw.get("observation_type")
    return m


def test_human_approval_pause_and_resume(db, agents):
    from app.services.synexia import capability_router as cr
    from app.models.execution import Plan, PlanNode

    upstream = PlanNode(
        id="pn-up", seq=0, node_type="agent", name="Writeback",
        description="", dependencies=[], status="pending",
        inputs={"task": "post PO", "agent_name": "Finance Agent"},
    )
    downstream = PlanNode(
        id="pn-down", seq=1, node_type="agent", name="Confirm",
        description="", dependencies=[0], status="pending",
        inputs={"task": "confirm PO", "agent_name": "Sales Agent"},
    )
    plan = Plan(id="plan-appr", execution_id="exec-appr", version=1, status="draft",
                summary="t", is_acyclic=True, nodes=[upstream, downstream])

    execution = MagicMock()
    execution.conversation_id = "c-appr"
    execution.id = "exec-appr"
    execution.app_id = "app-appr"
    execution.agent_name = "general_assistant"
    execution.policy_decision = {}
    execution.user_message = "x"
    execution.task_spec = {}

    child_calls = []

    async def _child(task, agent_name, db_, user_id_, max_iter, endpoint, context=None, agent_app=None, wall_seconds=None):
        child_calls.append(agent_name)
        return {
            "success": True, "status": "completed", "task": task,
            "agent_name": agent_name,
            "response": "draft" if agent_name == "Finance Agent" else "ok",
            "requires_approval": agent_name == "Finance Agent",
            "approval_reason": "high-value write" if agent_name == "Finance Agent" else None,
        }

    with patch.object(__import__("app.config", fromlist=["settings"]).settings, "PLAN_DAG_PARALLEL_ENABLED", False), \
         patch.object(dt, "_run_sub_agent", side_effect=_child), \
         patch.object(cr, "_record_observation", side_effect=lambda *a, **kw: _obs(kw)):
        # FIRST PASS: upstream runs and flags approval -> the run PAUSES.
        cr.execute_plan_nodes(db, execution, plan, "user-appr")
        assert upstream.status == "completed"
        assert execution.needs_approval is True
        # Downstream must NOT execute while awaiting approval.
        assert downstream.status == "pending"
        assert "Sales Agent" not in child_calls

        # RESUME: approval granted -> re-run. Upstream is already completed
        # (skipped), and the downstream now executes.
        execution.needs_approval = False
        cr.execute_plan_nodes(db, execution, plan, "user-appr")
        assert downstream.status == "completed"
        assert "Sales Agent" in child_calls
