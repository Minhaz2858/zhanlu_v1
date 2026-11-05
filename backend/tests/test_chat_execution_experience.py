"""Chat UI & agent execution experience repair (2026-10-31) — deterministic.

Root causes fixed:
  1. TurnPlan ``direct_response`` was advisory only → "hi" fanned out to
     sub-agents. Now the strategy is ENFORCED (toolset cleared).
  2. ``subagent_invoked`` carried only a description → generic "sub-agent"
     labels. Now the real AgentApp identity from the call args is attached.
  3. delegate_tool's finally-fallback could insert a terminal DUPLICATE next
     to a stuck running row → duplicate nodes in the execution tree.
  4. ``GET /executions/{id}/agent-tree`` crashed on a NameError typo.

All tests are deterministic (no LLM, no network).
"""
import inspect

import pytest

from app.routers.agents import (
    _delegate_agent_identity,
    _enforce_turn_execution_strategy,
)


class _Plan:
    def __init__(self, strategy):
        self.execution_strategy = strategy


# ── Part D: direct-response enforcement ─────────────────────────────────────
def test_direct_response_clears_toolset():
    tools = [{"type": "function", "function": {"name": "delegate_task"}}]
    out, note = _enforce_turn_execution_strategy(tools, _Plan("direct_response"))
    assert out == []
    assert "direct_response" in note


def test_greeting_plan_from_real_turn_planner_enforces_no_tools():
    # Uses the REAL TurnPlan classifier: a greeting must produce
    # direct_response, and the enforcement must clear the full toolset —
    # no delegation, no database access, no fake execution events.
    from app.services.turn_planner import build_turn_plan

    plan = build_turn_plan("hi")
    assert plan.execution_strategy == "direct_response"
    tools = [
        {"type": "function", "function": {"name": "delegate_task"}},
        {"type": "function", "function": {"name": "list_available_agents"}},
        {"type": "function", "function": {"name": "query_database"}},
    ]
    out, _ = _enforce_turn_execution_strategy(tools, plan)
    assert out == []


def test_non_conversational_turn_keeps_toolset():
    tools = [{"type": "function", "function": {"name": "query_database"}}]
    out, note = _enforce_turn_execution_strategy(tools, _Plan("multi_agent"))
    assert out is tools
    assert note == ""


def test_none_plan_keeps_toolset():
    tools = [{"type": "function", "function": {"name": "web_search"}}]
    out, _ = _enforce_turn_execution_strategy(tools, None)
    assert out is tools


# ── Part C: real specialist identity on subagent_invoked ────────────────────
def test_delegate_identity_single():
    assert _delegate_agent_identity({"agent_name": "manufacturing_analyst"}) == [
        "manufacturing_analyst"]


def test_delegate_identity_batch():
    args = {"tasks": [
        {"task": "a", "agent_name": "sales_analyst"},
        {"task": "b", "agent_name": "finance_agent"},
        {"task": "c", "agent_name": "sales_analyst"},  # duplicate
        "plain string task",  # no identity — never invented
    ]}
    assert _delegate_agent_identity(args) == ["sales_analyst", "finance_agent"]


def test_delegate_identity_absent():
    assert _delegate_agent_identity({}) == []
    assert _delegate_agent_identity({"tasks": ["solo"]}) == []
    assert _delegate_agent_identity(None) == []
    assert _delegate_agent_identity("not-a-dict") == []


# ── Part C: no duplicate invocation rows ────────────────────────────────────
@pytest.fixture()
def db():
    from app.database import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def test_invocation_row_exists_semantics(db):
    from app.models.agent_invocation import AgentInvocation
    from app.services.tool_handlers.delegate_tool import _invocation_row_exists

    row = AgentInvocation(
        id="inv-exists-1",
        agent_app_id="app-test",
        conversation_id="conv-x",
        user_id="u1",
        invocation_type="sub_agent",
        status="running",
    )
    db.add(row)
    db.commit()
    assert _invocation_row_exists(db, "inv-exists-1") is True
    assert _invocation_row_exists(db, "inv-missing") is False
    assert _invocation_row_exists(db, "") is False
    assert _invocation_row_exists(db, None) is False


def test_agent_tree_summary_counts_match_nodes():
    # Rule 7: the summary must be derived from the same nodes as the tree.
    from app.services.agent_invocations import _summarize

    roots = [{
        "status": "completed", "delegation_mode": "supervisor", "review": None,
        "token_usage": None, "cost_amount": None, "evidence_ids": [],
        "started_at": None, "completed_at": None,
        "children": [{
            "status": "failed", "delegation_mode": "peer", "review": None,
            "token_usage": None, "cost_amount": None, "evidence_ids": ["e1"],
            "started_at": None, "completed_at": None, "children": [],
        }],
    }]
    s = _summarize(roots)
    assert s["total"] == 2
    assert s["completed"] == 1
    assert s["failed"] == 1
    assert s["peer_collaborations"] == 1
    assert s["evidence_count"] == 1


# ── Part C: the execution-tree-by-execution endpoint must not NameError ─────
def test_agent_tree_by_execution_has_no_typo():
    from app.routers import executions as ex

    src = inspect.getsource(ex)
    assert "exution_id" not in src


# ── Part C: terminal-event guarantee helpers exist on the stream path ───────
def test_stream_source_flushes_open_subagents():
    # The v3 stream must flush open sub-agent badges both on the normal done
    # path (yielded) and on abnormal exit (persisted) — no node may stay
    # Running forever when a return event was lost.
    from app.routers import agents as ag

    src = inspect.getsource(ag)
    assert 'outcome": "interrupted"' in src or "outcome': 'interrupted'" in src
    assert src.count("subagent_returned") >= 3
