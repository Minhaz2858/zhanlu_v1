"""Unit tests for the extracted tool-batch execution core (P2-12).

Covers ``app.services.agent_loop.tool_executor``:
- ``execute_tool_batch``: single-call fast path, parallel multi-call,
  guard-blocked synthetic results, exception isolation.
- ``emit_tool_progress_while_waiting``: yields ``tool_progress`` frames
  while the task is pending, exits once the task completes.
- ``is_long_running_tool``: membership against the provided set.
"""
import asyncio
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest

from app.services.agent_loop.tool_executor import (
    emit_tool_progress_while_waiting,
    execute_tool_batch,
    is_long_running_tool,
)


class _Decision:
    def __init__(self, allows: bool):
        self.allows_execution = allows


def _call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"tool_name": name, "args": args, "tool_call_id": call_id, "args_str": json.dumps(args)}


# ---------------------------------------------------------------------------
# execute_tool_batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_call_fast_path():
    calls = [_call("query", {"q": 1})]
    seen = []

    async def invoke(name, args):
        seen.append((name, args))
        return {"success": True, "tool_name": name}

    results = await execute_tool_batch(
        calls, before_call=lambda n, a: _Decision(True), invoke=invoke
    )
    assert results == [{"success": True, "tool_name": "query"}]
    assert seen == [("query", {"q": 1})]


@pytest.mark.asyncio
async def test_multi_call_parallel_order_preserved():
    calls = [
        _call("a", {"n": 1}, call_id="ca"),
        _call("b", {"n": 2}, call_id="cb"),
        _call("c", {"n": 3}, call_id="cc"),
    ]
    order = []

    async def invoke(name, args):
        await asyncio.sleep(0.01 * (3 - args["n"]))  # reverse completion order
        order.append(name)
        return {"success": True, "tool_name": name}

    results = await execute_tool_batch(
        calls, before_call=lambda n, a: _Decision(True), invoke=invoke
    )
    # Input order preserved even though completion happened in reverse.
    assert [r["tool_name"] for r in results] == ["a", "b", "c"]
    assert order == ["c", "b", "a"]


@pytest.mark.asyncio
async def test_guard_blocked_call_uses_synthetic_result():
    calls = [_call("write", {"danger": True})]

    async def invoke(name, args):
        return {"success": True}

    def blocked_result_factory(gd):
        return json.dumps({"success": False, "error": "blocked", "guardrail": True})

    results = await execute_tool_batch(
        calls,
        before_call=lambda n, a: _Decision(False),
        invoke=invoke,
        blocked_result_factory=blocked_result_factory,
    )
    assert results == [{"success": False, "error": "blocked", "guardrail": True}]


@pytest.mark.asyncio
async def test_guard_blocked_without_factory_returns_error_dict():
    calls = [_call("write", {})]

    async def invoke(name, args):
        return {"success": True}

    results = await execute_tool_batch(
        calls, before_call=lambda n, a: _Decision(False), invoke=invoke
    )
    assert results == [{"success": False, "error": "blocked by guardrail"}]


@pytest.mark.asyncio
async def test_exception_is_isolated_and_normalized():
    calls = [_call("good", {}), _call("bad", {})]

    async def invoke(name, args):
        if name == "bad":
            raise ValueError("boom")
        return {"success": True, "tool_name": name}

    results = await execute_tool_batch(
        calls, before_call=lambda n, a: _Decision(True), invoke=invoke
    )
    assert results[0] == {"success": True, "tool_name": "good"}
    assert results[1]["success"] is False
    assert results[1]["error"].startswith("ValueError: boom")


@pytest.mark.asyncio
async def test_single_call_exception_propagates():
    # The single-call fast path intentionally propagates (matches the
    # original v2/resume/v3 behavior - only the parallel gather path
    # isolates exceptions).
    calls = [_call("bad", {})]

    async def invoke(name, args):
        raise RuntimeError("single boom")

    with pytest.raises(RuntimeError, match="single boom"):
        await execute_tool_batch(
            calls, before_call=lambda n, a: _Decision(True), invoke=invoke
        )


# ---------------------------------------------------------------------------
# emit_tool_progress_while_waiting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_progress_frames_yielded_while_running_then_stop():
    async def worker():
        await asyncio.sleep(0.05)
        return "done"

    task = asyncio.create_task(worker())
    calls = [
        {"tool_name": "query", "tool_call_id": "t1", "args_str": "{}"},
        {"tool_name": "write", "tool_call_id": "t2", "args_str": '{"k": 1}'},
    ]
    frames = []
    async for frame in emit_tool_progress_while_waiting(task, calls, interval=0.01):
        frames.append(frame)
    await task
    assert len(frames) >= 1
    payload = json.loads(frames[0].removeprefix("data: ").strip())
    assert payload["type"] == "tool_progress"
    assert [c["id"] for c in payload["tool_calls"]] == ["t1", "t2"]
    assert all(c["status"] == "running" for c in payload["tool_calls"])
    # Ends with the trailing SSE blank line.
    assert frames[0].endswith("\n\n")


@pytest.mark.asyncio
async def test_progress_uses_display_names():
    async def worker():
        await asyncio.sleep(0.05)
        return "done"

    task = asyncio.create_task(worker())
    calls = [{"tool_name": "query", "tool_call_id": "t1", "args_str": ""}]
    frames = []
    async for frame in emit_tool_progress_while_waiting(
        task, calls, interval=0.01, display_names={"query": "Query Data"}
    ):
        frames.append(frame)
    await task
    payload = json.loads(frames[0].removeprefix("data: ").strip())
    assert payload["tool_calls"][0]["name"] == "Query Data"


# ---------------------------------------------------------------------------
# is_long_running_tool
# ---------------------------------------------------------------------------

def test_is_long_running_tool_membership():
    long_running = {"web_extract", "execute_query"}
    assert is_long_running_tool("web_extract", long_running) is True
    assert is_long_running_tool("memory", long_running) is False
    assert is_long_running_tool("unknown", set()) is False
