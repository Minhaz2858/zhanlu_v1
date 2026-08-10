"""Tests for AgentRunOrchestrator — DI-based unified agent loop."""
from __future__ import annotations

import pytest

from app.services.harness.orchestrator import (
    AgentRunOrchestrator,
    RunResult,
)


# ---------------------------------------------------------------------------
# Fake LLM / tool dispatch helpers
# ---------------------------------------------------------------------------

def _make_llm(responses: list[dict]):
    """Return an async llm_fn that yields canned responses in order.

    Each response dict: {"content": str, "tool_calls": list}.
    The last response is repeated if the loop asks for more.
    """
    idx = [0]  # mutable counter

    async def llm(messages, tools, temperature):
        i = min(idx[0], len(responses) - 1)
        idx[0] += 1
        return responses[i]

    return llm


async def _noop_dispatcher(tool_name, args, db, user_id, context):
    return {"success": True, "result": f"called {tool_name}"}


async def _failing_dispatcher(tool_name, args, db, user_id, context):
    raise RuntimeError("simulated dispatch failure")


# ---------------------------------------------------------------------------
# RunResult dataclass
# ---------------------------------------------------------------------------

def test_run_result_defaults():
    r = RunResult(run_id="abc123", success=True, answer="hello")
    assert r.run_id == "abc123"
    assert r.answer == "hello"
    assert r.success is True
    assert r.tool_calls == []
    assert r.iterations == 0
    assert r.error is None


# ---------------------------------------------------------------------------
# Basic loop: immediate prose answer (no tool calls)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_immediate_answer_returns_run_result():
    orch = AgentRunOrchestrator(
        agent_name="test_agent",
        task="What is 2+2?",
        system_prompt="You are a helpful assistant.",
        tool_schemas=[],
        allowed_tools=set(),
        llm_fn=_make_llm([
            {"content": "4", "tool_calls": []},
        ]),
        tool_dispatcher=_noop_dispatcher,
        max_iterations=5,
    )
    result = await orch.run()
    assert isinstance(result, RunResult)
    assert result.success is True
    assert result.answer == "4"
    assert result.iterations == 1
    assert len(result.tool_calls) == 0


# ---------------------------------------------------------------------------
# Tool-call loop: one tool call → final answer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_tool_call_loop():
    orch = AgentRunOrchestrator(
        agent_name="test_agent",
        task="Look up the weather",
        system_prompt="You are helpful.",
        tool_schemas=[{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
        allowed_tools={"get_weather"},
        llm_fn=_make_llm([
            {
                "content": "Let me check.",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {"name": "get_weather", "arguments": '{"city":"NYC"}'},
                }],
            },
            {"content": "It's 72F in NYC.", "tool_calls": []},
        ]),
        tool_dispatcher=_noop_dispatcher,
        max_iterations=8,
    )
    result = await orch.run()
    assert result.success is True
    assert "72F" in result.answer
    assert result.iterations == 2
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["tool"] == "get_weather"


# ---------------------------------------------------------------------------
# denied_tools enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_denied_tool_blocked():
    orch = AgentRunOrchestrator(
        agent_name="test_agent",
        task="Do something recursive",
        system_prompt="You are helpful.",
        tool_schemas=[{"type": "function", "function": {"name": "dangerous", "parameters": {}}}],
        allowed_tools={"dangerous"},
        denied_tools={"dangerous"},
        llm_fn=_make_llm([
            {
                "content": "Calling dangerous tool.",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {"name": "dangerous", "arguments": "{}"},
                }],
            },
            {"content": "Done after failed call.", "tool_calls": []},
        ]),
        tool_dispatcher=_noop_dispatcher,
        max_iterations=5,
    )
    result = await orch.run()
    assert result.success is True
    # The tool call was intercepted (error returned but not fatal)
    assert any(
        tc["tool"] == "dangerous" and tc.get("result_success") is False
        for tc in result.tool_calls
    )


# ---------------------------------------------------------------------------
# Tool not in allowed_tools → blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unauthorized_tool_blocked():
    orch = AgentRunOrchestrator(
        agent_name="test_agent",
        task="Try unauthorized tool",
        system_prompt="You are helpful.",
        tool_schemas=[],
        allowed_tools={"safe_tool"},
        denied_tools=set(),
        llm_fn=_make_llm([
            {
                "content": "Calling other_tool.",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {"name": "other_tool", "arguments": "{}"},
                }],
            },
            {"content": "Done.", "tool_calls": []},
        ]),
        tool_dispatcher=_noop_dispatcher,
        max_iterations=5,
    )
    result = await orch.run()
    assert result.success is True
    assert any(
        tc["tool"] == "other_tool" and tc.get("result_success") is False
        for tc in result.tool_calls
    )


# ---------------------------------------------------------------------------
# Max iteration bounding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_iterations_bound():
    """When every turn produces tool calls, the loop stops at max_iterations."""
    tool_call = {
        "id": "call_x",
        "function": {"name": "loop_tool", "arguments": "{}"},
    }
    orch = AgentRunOrchestrator(
        agent_name="test_agent",
        task="Loop forever",
        system_prompt="You are helpful.",
        tool_schemas=[{"type": "function", "function": {"name": "loop_tool", "parameters": {}}}],
        allowed_tools={"loop_tool"},
        llm_fn=_make_llm([
            {"content": "Loop 1", "tool_calls": [tool_call]},
            {"content": "Loop 2", "tool_calls": [tool_call]},
            {"content": "Loop 3", "tool_calls": [tool_call]},
            {"content": "Loop 4", "tool_calls": [tool_call]},
        ]),
        tool_dispatcher=_noop_dispatcher,
        max_iterations=3,
    )
    result = await orch.run()
    # 3 loop iterations + 1 synthesis turn = 4
    assert result.iterations == 4
    assert result.answer != ""  # synthesis turn should produce text


# ---------------------------------------------------------------------------
# Forced synthesis turn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthesis_turn_when_no_final_text():
    """When the budget exhausts with tool calls but no prose, force a synthesis turn."""
    tool_call = {
        "id": "call_s",
        "function": {"name": "slow_tool", "arguments": "{}"},
    }
    orch = AgentRunOrchestrator(
        agent_name="test_agent",
        task="Do something",
        system_prompt="You are helpful.",
        tool_schemas=[{"type": "function", "function": {"name": "slow_tool", "parameters": {}}}],
        allowed_tools={"slow_tool"},
        llm_fn=_make_llm([
            {"content": "Still working", "tool_calls": [tool_call]},
            {"content": "Still working", "tool_calls": [tool_call]},
            # Synthesis call: no tools
            {"content": "Here is the synthesis.", "tool_calls": []},
        ]),
        tool_dispatcher=_noop_dispatcher,
        max_iterations=2,
    )
    result = await orch.run()
    assert "synthesis" in result.answer.lower()
    # 2 loop iterations + 1 synthesis = 3
    assert result.iterations == 3


# ---------------------------------------------------------------------------
# run_store callback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_store_receives_start_and_finish():
    events = []

    def store(event, payload):
        events.append((event, payload))

    orch = AgentRunOrchestrator(
        agent_name="test_agent",
        task="hello",
        system_prompt="You are helpful.",
        tool_schemas=[],
        allowed_tools=set(),
        llm_fn=_make_llm([{"content": "hi", "tool_calls": []}]),
        tool_dispatcher=_noop_dispatcher,
        run_store=store,
        max_iterations=3,
    )
    await orch.run()
    assert len(events) == 2
    assert events[0][0] == "start"
    assert events[1][0] == "finish"
    assert events[1][1]["success"] is True
    assert events[1][1]["answer"] == "hi"


# ---------------------------------------------------------------------------
# run_store failure is non-fatal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_store_failure_does_not_break_loop():
    def store(event, payload):
        raise RuntimeError("simulated persistence failure")

    orch = AgentRunOrchestrator(
        agent_name="test_agent",
        task="hello",
        system_prompt="You are helpful.",
        tool_schemas=[],
        allowed_tools=set(),
        llm_fn=_make_llm([{"content": "ok", "tool_calls": []}]),
        tool_dispatcher=_noop_dispatcher,
        run_store=store,
        max_iterations=3,
    )
    result = await orch.run()
    assert result.success is True
    assert result.answer == "ok"


# ---------------------------------------------------------------------------
# LLM failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_failure_returns_error_result():
    async def broken_llm(messages, tools, temperature):
        raise RuntimeError("simulated LLM outage")

    orch = AgentRunOrchestrator(
        agent_name="test_agent",
        task="crash",
        system_prompt="You are helpful.",
        tool_schemas=[],
        allowed_tools=set(),
        llm_fn=broken_llm,
        tool_dispatcher=_noop_dispatcher,
        max_iterations=3,
    )
    result = await orch.run()
    assert result.success is False
    assert result.error is not None
    assert "could not reach" in result.answer.lower()


# ---------------------------------------------------------------------------
# Tool dispatch failure is non-fatal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_dispatch_failure_captured():
    orch = AgentRunOrchestrator(
        agent_name="test_agent",
        task="Use a flaky tool",
        system_prompt="You are helpful.",
        tool_schemas=[{"type": "function", "function": {"name": "flaky", "parameters": {}}}],
        allowed_tools={"flaky"},
        llm_fn=_make_llm([
            {
                "content": "Calling flaky.",
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "flaky", "arguments": "{}"},
                }],
            },
            {"content": "Recovered.", "tool_calls": []},
        ]),
        tool_dispatcher=_failing_dispatcher,
        max_iterations=5,
    )
    result = await orch.run()
    # Tool result captured as error, loop continues
    assert result.success is True
    assert any(
        tc["tool"] == "flaky" and tc.get("result_success") is False
        for tc in result.tool_calls
    )
    assert "Recovered" in result.answer
