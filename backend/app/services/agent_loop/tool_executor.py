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

from .tool_pipeline import (
    DEFAULT_SUB_CALL_LIMIT,
    PipelineState,
    activate_pipeline,
    apply_post_transforms,
    guard_verdict,
)

logger = logging.getLogger(__name__)


def _guard(call: dict, before_call) -> tuple[bool, str, object]:
    """Evaluate the loop guard for one call. Returns ``(allows, reason, decision)``.

    A guard that raises is a guard that cannot answer — the call is refused
    (fail-closed) instead of escaping as an unhandled exception.
    """
    try:
        decision = before_call(call["tool_name"], call["args"])
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("Guard raised for %s: %s", call.get("tool_name"), exc)
        return False, f"guard failed: {exc}", None
    allows, reason = guard_verdict(decision)
    return allows, reason, decision


def _blocked_result(call: dict, decision, reason: str, blocked_result_factory=None) -> dict:
    """Turn a guard denial into one result dict.

    ``blocked_result_factory`` (``synthetic_blocked_result``) produces the
    loop's canonical blocked payload from the guard's own decision. If it is
    absent — or cannot read a decision shape it does not understand — the call
    is still refused, with the guard's reason.
    """
    if blocked_result_factory is not None:
        try:
            return json.loads(blocked_result_factory(decision))
        except Exception as exc:  # noqa: BLE001 — never let the factory unblock a call
            logger.warning(
                "blocked_result_factory could not render a denial for %s: %s",
                call.get("tool_name"), exc,
            )
    return {"success": False, "error": reason or "blocked by guardrail"}


# Tools whose execution MUST be serialized in call order.
#
# These drive ONE shared Fusion 360 document through a single-socket bridge
# (host.docker.internal:9876). Running them concurrently makes the ORDER
# nondeterministic, and their results carry POSITIONAL HANDLES (SKETCH_INDEX /
# BODY_INDEX) that the calling model must map back to its own plan.
#
# Observed failure (2026-09-16, CAD Agent building a hex bolt): the model
# batched three fusion360_sketch_create calls in order; the results came back
# SKETCH_INDEX 1, 0, 2, and the next fusion360_info reported SKETCHES: 4. The
# model could no longer tell which sketch held the hex-head profile, cleared
# the design, and restarted -- five identical build-and-wipe cycles, each one
# extruding a body and then discarding it. Serializing in call order is both
# the fix for the index scramble and the correct semantics for a
# single-document, single-socket backend.
_SERIAL_TOOL_PREFIXES = ("fusion360_",)


def _requires_serial(parsed_calls) -> bool:
    for c in parsed_calls or []:
        if str(c.get("tool_name") or "").startswith(_SERIAL_TOOL_PREFIXES):
            return True
    return False


async def execute_tool_batch(
    parsed_calls: list,
    *,
    before_call,
    invoke,
    blocked_result_factory=None,
    db=None,
    user_id=None,
    context=None,
    sub_call_limit: int = DEFAULT_SUB_CALL_LIMIT,
) -> list:
    """Run a batch of tool calls in parallel with guard + exception isolation.

    The whole batch runs inside an *active pipeline*
    (:mod:`app.services.agent_loop.tool_pipeline`), so nested callers — the
    ``execute_code`` PTC bridge — dispatch their sub-calls through this same
    guard + registry path instead of a second, weaker dispatcher. Every result
    passes the registered post-execute transforms before it is returned.

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
        db: database session — exposed to sub-calls for permission checks.
        user_id: acting user id — exposed to sub-calls.
        context: runtime context (conversation_id, agent_name, progress
            emitter) — exposed to sub-calls and used for UI activity steps.
        sub_call_limit: max sub-calls a single code block may dispatch.

    Returns:
        Ordered list of result dicts, one per input call. On the multi-call
        (serial / parallel) paths, exceptions raised by ``invoke`` are
        converted to ``{"success": False, "error": ...}``. The SINGLE-call
        fast path is deliberately the exception: it PROPAGATES the exception
        (matches the original v2/resume/v3 behavior — see
        ``test_single_call_exception_propagates``).
    """
    state = PipelineState(
        invoke=invoke,
        before_call=before_call,
        blocked_result_factory=blocked_result_factory,
        db=db,
        user_id=user_id,
        context=context,
        sub_call_limit=sub_call_limit,
    )
    with activate_pipeline(state):
        results = await _execute_batch(
            parsed_calls,
            before_call=before_call,
            invoke=invoke,
            blocked_result_factory=blocked_result_factory,
        )
    return results


def _finish_result(call: dict, result: dict) -> dict:
    """Apply the post-execute seam to one result (contained, never raises)."""
    if not isinstance(result, dict):
        return result
    transformed, _contexts = apply_post_transforms(
        call.get("tool_name", ""), call.get("args") or {}, result
    )
    return transformed


async def _execute_batch(
    parsed_calls: list,
    *,
    before_call,
    invoke,
    blocked_result_factory=None,
) -> list:
    """The pre-P2-13 batch mechanics (unchanged semantics)."""
    if len(parsed_calls) == 1:
        call = parsed_calls[0]
        allows, reason, decision = _guard(call, before_call)
        if allows:
            return [_finish_result(call, await invoke(call["tool_name"], call["args"]))]
        return [_blocked_result(call, decision, reason, blocked_result_factory)]

    # A batch touching a serialized backend runs IN CALL ORDER, never
    # concurrently -- see _SERIAL_TOOL_PREFIXES. Same guard + isolation as
    # the parallel path, so failures still degrade to a per-call error dict
    # instead of taking the batch down.
    if _requires_serial(parsed_calls):
        _serial_results: list = []
        for _c in parsed_calls:
            try:
                _serial_results.append(await _exec_one_serial(_c, before_call, invoke, blocked_result_factory))
            except Exception as _exc:
                logger.warning(
                    "Tool '%s' raised (serial): %s", _c.get("tool_name"), _exc
                )
                _serial_results.append(
                    {"success": False, "error": f"{type(_exc).__name__}: {_exc}"}
                )
        return _serial_results

    async def _exec_one(call):
        allows, reason, decision = _guard(call, before_call)
        if allows:
            return _finish_result(call, await invoke(call["tool_name"], call["args"]))
        return _blocked_result(call, decision, reason, blocked_result_factory)

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


async def _exec_one_serial(call, before_call, invoke, blocked_result_factory):
    """Guard + invoke for ONE call — used by the serialized batch path.

    Mirrors the ``_exec_one`` closure inside ``_execute_batch`` but lives at
    module level so the serial branch can call it before that closure is bound.
    """
    allows, reason, decision = _guard(call, before_call)
    if allows:
        return _finish_result(call, await invoke(call["tool_name"], call["args"]))
    return _blocked_result(call, decision, reason, blocked_result_factory)


async def emit_tool_progress_while_waiting(
    task: asyncio.Task,
    parsed_calls: list,
    interval: float = 5.0,
    display_names: dict | None = None,
    extra_frames: list | None = None,
):
    """Yield ``tool_progress`` SSE frames while ``task`` is still running.

    The tool-execution task keeps running in the background; every
    ``interval`` seconds we yield a ``data: {...}`` chunk that marks the
    in-flight tool calls as ``status: running``. This is additive — the
    frontend already renders ``tool_progress`` events and treats entries
    without a ``results`` key as in-flight.

    ``extra_frames`` (multi-agent orchestration milestone, 2026-11-01): an
    optional list that long-running tool handlers (delegate_task /
    ask_data_agent) append structured agent-execution SSE frames onto while
    they run. Each tick drains and yields everything accumulated so far so
    the Agent Execution panel updates LIVE during the delegation instead of
    only after the tool batch returns. Drained frames are removed from the
    list so the post-batch drain in the router does not re-emit them.
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
        if extra_frames:
            while extra_frames:
                yield extra_frames.pop(0)
        if done:
            break
        yield f'data: {json.dumps({"type": "tool_progress", "tool_calls": running_calls})}\n\n'
    # Final drain: frames appended right before the task finished.
    if extra_frames:
        while extra_frames:
            yield extra_frames.pop(0)


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
