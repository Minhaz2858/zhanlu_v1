"""Tests for the swarm-backed ask_data_agent path (IBM agent-selection).

C1: the builtin data_agent definition covers the data-tool whitelist and the
    DATA_AGENT_SWARM_ENABLED flag defaults False.
C2: when the flag is on, _ask_data_agent routes through the swarm runner;
    the dispatcher enforces the whitelist, injects the default data_source_id,
    captures rows into state; flag off / swarm failure fall back to the
    legacy loop.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import settings
from app.services.tool_handlers import delegation_tools as dt


def test_data_agent_definition_exists_with_tool_whitelist():
    from app.services.agent_definitions import get_agent_definition

    d = get_agent_definition("data_agent")
    assert d is not None
    assert d.system_prompt  # own prompt
    tools = set(d.tools or [])
    assert set(dt._DATA_AGENT_TOOLS) <= tools  # whitelist covers the data tools


def test_swarm_flag_defaults_false():
    assert settings.DATA_AGENT_SWARM_ENABLED is False


@pytest.fixture(autouse=True)
def _reset_flag(monkeypatch):
    # Deterministic gate: planner/fastpath/router LLM+DB paths off so the
    # swarm gate is the only branch under test.
    monkeypatch.setattr(settings, "DATA_AGENT_PLANNER_ENABLED", False)
    monkeypatch.setattr(settings, "DATA_AGENT_FASTPATH_ENABLED", False)
    monkeypatch.setattr(settings, "KG_RESOURCE_ROUTER_ENABLED", False)
    yield
    settings.DATA_AGENT_SWARM_ENABLED = False


# ---------------------------------------------------------------- gate

@pytest.mark.asyncio
async def test_flag_off_skips_swarm_path(monkeypatch):
    settings.DATA_AGENT_SWARM_ENABLED = False
    monkeypatch.setattr(settings, "DATA_AGENT_FASTPATH_ENABLED", True)
    called = {"swarm": False}

    async def _no_swarm(*a, **k):
        called["swarm"] = True
        return {"success": True, "answer": "x"}

    monkeypatch.setattr(dt, "_run_swarm_data_agent", _no_swarm)
    # fastpath returns a result so the legacy loop is never reached either
    async def _fastpath(*a, **k):
        return {"success": True, "answer": "fastpath", "rows": [[1]]}

    monkeypatch.setattr(dt, "_try_nl_fastpath", _fastpath)
    result = await dt._ask_data_agent(
        {"question": "how many lots?"}, None, "u", {"bound_kb_ids": ["kb1"]})
    assert result.get("answer") == "fastpath"
    assert called["swarm"] is False


@pytest.mark.asyncio
async def test_flag_on_routes_through_swarm(monkeypatch):
    settings.DATA_AGENT_SWARM_ENABLED = True
    seen = {}

    async def _swarm(question, bound_kb_ids, preferred_kb, db, user_id, context, endpoint, max_iterations=6):
        seen.update(question=question, preferred=preferred_kb)
        return {"success": True, "answer": "42", "rows": [[42]]}

    monkeypatch.setattr(dt, "_run_swarm_data_agent", _swarm)
    result = await dt._ask_data_agent(
        {"question": "how many lots?", "data_source_id": "kb9"},
        None, "u", {"bound_kb_ids": ["kb9"]})
    assert seen["question"] == "how many lots?"
    assert seen["preferred"] == "kb9"
    assert result["answer"] == "42"


@pytest.mark.asyncio
async def test_swarm_failure_falls_back_to_loop(monkeypatch):
    settings.DATA_AGENT_SWARM_ENABLED = True

    async def _boom(*a, **k):
        raise RuntimeError("swarm exploded")

    monkeypatch.setattr(dt, "_run_swarm_data_agent", _boom)
    # legacy loop fallback: stub the LLM call to return a final text (no tool calls)
    async def _no_tools(messages, tools, **kw):
        return {"response": "legacy loop answer", "tool_calls": []}

    monkeypatch.setattr(dt, "_call_llm_with_retry", _no_tools)
    monkeypatch.setattr(dt, "pre_call_prep", lambda *a, **k: None)
    monkeypatch.setattr(dt, "is_metadata_only_rows", lambda rows, question=None: False)
    monkeypatch.setattr(dt, "_build_sub_agent_prompt", lambda *a, **k: "sys")
    monkeypatch.setattr(dt, "_build_sub_agent_tool_schemas", lambda: [])
    # NL2SQL retry + synthesis paths are skipped: no metadata rows, no truncated
    result = await dt._ask_data_agent(
        {"question": "how many lots?", "max_iterations": 1},
        None, "u", {"bound_kb_ids": ["kb1"]})
    # The legacy loop ran (its no-rows directive fallback fired) — the point
    # is the swarm path did NOT serve this call.
    assert result.get("swarm") is not True
    assert "could not retrieve any business data" in result["answer"]


# ---------------------------------------------------------------- dispatcher

class _FakeRuntime:
    """Captures the llm_fn/dispatcher wiring; executes one real dispatcher
    call (like the real runtime would), then returns a canned result."""

    last_instance = None

    async def run(self, agent_name, task, llm_fn, tool_dispatcher, db=None,
                  user_id=None, member_name=None, max_iterations=8, use_harness=None):
        _FakeRuntime.last_instance = self
        self.llm_fn = llm_fn
        self.dispatcher = tool_dispatcher
        self.agent_name = agent_name
        # Simulate the loop executing one whitelisted tool call.
        await tool_dispatcher("execute_query", {"sql": "SELECT COUNT(*) FROM x"}, db, user_id, None)
        return SimpleNamespace(
            member_name="data_agent", agent_name=agent_name, task=task,
            tool_calls=[{"tool": "execute_query"}], success=True,
            final_response="16,143 lots", error=None,
        )


@pytest.mark.asyncio
async def test_dispatcher_enforces_whitelist_and_injects_source(monkeypatch):
    captured = {}

    async def fake_execute_tool(name, call_args, _db, _uid, _ctx):
        captured.update(name=name, args=call_args, ctx=_ctx)
        return {"success": True, "rows": [{"n": 16143}], "sql": "SELECT 1",
                "source": {"id": "kb9", "name": "ERP"}}

    import app.services.agent_tools as agent_tools
    monkeypatch.setattr(agent_tools, "execute_tool", fake_execute_tool)
    monkeypatch.setattr(dt, "_build_sub_agent_tool_schemas", lambda: [])

    async def _no_llm(messages, tools, **kw):
        return {"response": "", "tool_calls": []}

    monkeypatch.setattr(dt, "_call_llm_with_retry", _no_llm)

    with patch("app.services.swarm.runtime.SwarmRuntime", _FakeRuntime):
        result = await dt._run_swarm_data_agent(
            "how many lots?", ["kb9"], "kb9", None, "u", {}, None)

    rt = _FakeRuntime.last_instance
    assert rt.agent_name == "data_agent"

    # whitelist enforcement
    blocked = await rt.dispatcher("write_file", {}, None, "u", None)
    assert blocked["success"] is False and "not allowed" in blocked["error"]

    # data_source_id injection when missing
    await rt.dispatcher("execute_query", {"sql": "SELECT COUNT(*) FROM x"}, None, "u", None)
    assert captured["args"]["data_source_id"] == "kb9"
    assert captured["name"] == "execute_query"

    # rows captured into the return shape
    assert result["rows"] == [{"n": 16143}]
    assert result["source_id"] == "kb9"
    assert result["swarm"] is True
    assert result["answer"] == "16,143 lots"


@pytest.mark.asyncio
async def test_swarm_empty_result_returns_none(monkeypatch):
    class _EmptyRuntime:
        async def run(self, *a, **k):
            return SimpleNamespace(
                member_name="d", agent_name="data_agent", task="t",
                tool_calls=[], success=False, final_response="", error="LLM error",
            )

    monkeypatch.setattr(dt, "_build_sub_agent_tool_schemas", lambda: [])
    with patch("app.services.swarm.runtime.SwarmRuntime", _EmptyRuntime):
        result = await dt._run_swarm_data_agent("q", [], None, None, "u", {}, None)
    assert result is None
