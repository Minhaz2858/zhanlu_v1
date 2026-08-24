"""Tool-batch execution core for the agent loop (P2-12 extraction).

Extracted from the three parallel loops in ``app/routers/agents.py``
(v2 / resume / v3 ``_stream_llm_with_tools``). The router keeps its
per-loop orchestration (approval pauses, smart retry, artifact tracking)
and delegates the shared mechanics — parallel execution, guard-check,
exception isolation, progress frames — to this module.

Semantics are identical to the original ``_run_tool_batch`` nested
closure:

- single-call fast path (no ``asyncio.gather``)
- ``before_call(tool_name, args)`` returns a guard decision exposing
  ``allows_execution``; blocked calls are replaced by
  ``blocked_result_factory(decision)`` (e.g. ``synthetic_blocked_result``)
- every exception is isolated per call and normalized to
  ``{"success": False, "error": "<TypeName>: <msg>"}``
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


async def execute_tool_batch(
    parsed_calls: list,
    *,
    before_call,
    invoke,
    blocked_result_factory=None,
) -> list:
    """Run a batch of tool calls in parallel with guard + exception isolation.

    Args:
        parsed_calls: list of ``{"tool_name", "args", "tool_call_id",
            "args_str", ...}`` dicts.
        before_call: sync callable ``(tool_name, args) -> decision`` where
            ``decision.allows_execution`` gates execution.
        invoke: async callable ``(tool_name, args) -> result dict``.
        blocked_result_factory: callable ``(decision) -> str`` returning a
            serialized synthetic result for blocked calls
            (``json.loads``-ed into the result list). Optional when every
            call passes the guard.

    Returns:
        Ordered list of result dicts, one per input call. Exceptions raised
        by ``invoke`` are converted to ``{"success": False, "error": ...}``.
    """
    if len(parsed_calls) == 1:
        call = parsed_calls[0]
        gd = before_call(call["tool_name"], call["args"])
        if gd.allows_execution:
            return [await invoke(call["tool_name"], call["args"])]
        if blocked_result_factory is None:
            return [{"success": False, "error": "blocked by guardrail"}]
        return [json.loads(blocked_result_factory(gd))]

    async def _exec_one(call):
        gd = before_call(call["tool_name"], call["args"])
        if gd.allows_execution:
            return await invoke(call["tool_name"], call["args"])
        if blocked_result_factory is None:
            return {"success": False, "error": "blocked by guardrail"}
        return json.loads(blocked_result_factory(gd))

    raw_results = await asyncio.gather(
        *[_exec_one(c) for c in parsed_calls], return_exceptions=True
    )
    results: list = []
    for i, r in enumerate(raw_results):
        if isinstance(r, Exception):
            logger.warning("Tool '%s' raised: %s", parsed_calls[i]["tool_name"], r)
            results.append({"success": False, "error": f"{type(r).__name__}: {r}"})
        else:
            results.append(r)
    return results


async def emit_tool_progress_while_waiting(
    task: asyncio.Task,
    parsed_calls: list,
    interval: float = 5.0,
    display_names: dict | None = None,
):
    """Yield ``tool_progress`` SSE frames while ``task`` is still running.

    The tool-execution task keeps running in the background; every
    ``interval`` seconds we yield a ``data: {...}`` chunk that marks the
    in-flight tool calls as ``status: running``. This is additive — the
    frontend already renders ``tool_progress`` events and treats entries
    without a ``results`` key as in-flight.
    """
    display_names = display_names or {}
    running_calls = [
        {
            "id": c.get("tool_call_id", ""),
            "name": display_names.get(c.get("tool_name", ""), c.get("tool_name", "")),
            "arguments_string": c.get("args_str", ""),
            "status": "running",
        }
        for c in parsed_calls
    ]
    while not task.done():
        done, _pending = await asyncio.wait({task}, timeout=interval)
        if done:
            break
        yield f'data: {json.dumps({"type": "tool_progress", "tool_calls": running_calls})}\n\n'


def is_long_running_tool(tool_name: str, long_running_tools) -> bool:
    """True if ``tool_name`` may run for tens of seconds or minutes.

    ``long_running_tools`` is the router-owned set of tool names
    (``_LONG_RUNNING_TOOLS``); kept as a parameter to avoid an import
    cycle between the agents router and this package.
    """
    return tool_name in long_running_tools


__all__ = [
    "execute_tool_batch",
    "emit_tool_progress_while_waiting",
    "is_long_running_tool",
]
