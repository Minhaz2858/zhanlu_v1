"""delegate_task tool — spawn a subagent with isolated context.

Creates a temporary sub-conversation, runs the agent runtime loop with a
restricted toolset (no delegate_task, no memory, no send_message to prevent
infinite recursion), and returns the final text response.

Supports batch mode: pass multiple tasks as a list to run them concurrently
via asyncio.gather().
"""

import asyncio
import json
import logging
import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app.services.llm_router import LLMEndpoint
from app.services.tool_registry import registry
from app.services.sub_agent_reliability import (
    call_llm_with_reliability,
    pre_call_prep,
    persist_result_str,
    apply_turn_budget_to_batch,
    ToolLoopGuardController,
    synthetic_blocked_result,
    IterationBudget,
    metrics,
)

logger = logging.getLogger(__name__)

# Tools that sub-agents CANNOT use (prevent infinite recursion and loops)
_RESTRICTED_TOOLS = {"delegate_task", "memory", "send_message"}


def _normalize_subagent_schema(name: str, schema: dict, description: str = "") -> dict:
    """Wrap a registry schema in the OpenAI function envelope.

    Most registry entries are already wrapped (``{type: "function",
    function: {...}}``). Two shapes are NOT and get rejected by
    DeepSeek/OpenAI with ``400 tools[N].type: unknown variant 'object'``:

    1. Bare parameters dicts — the ``universal_*`` DB-agnostic data tools
       register ``{type: "object", properties, required}`` (no name).
    2. Flat form — ``collect_enterprise_data`` is
       ``{name, description, parameters}`` with no top-level ``type``.

    This normalizes all three shapes to the envelope form.
    """
    if not isinstance(schema, dict):
        return schema
    if schema.get("type") == "function" and "function" in schema:
        return schema
    if schema.get("name") or "parameters" in schema:
        return {
            "type": "function",
            "function": {
                "name": schema.get("name") or name,
                "description": schema.get("description", ""),
                "parameters": schema.get("parameters")
                or {k: v for k, v in schema.items() if k not in ("name", "description")},
            },
        }
    # Bare parameters dict (universal_* tools)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or "",
            "parameters": schema,
        },
    }


async def _delegate_task(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    task = args.get("task", "").strip()
    agent_name = args.get("agent_name", "general_assistant")
    max_iterations = min(args.get("max_iterations", settings.DELEGATE_MAX_ITERATIONS), 10)
    endpoint: LLMEndpoint | None = (context or {}).get("endpoint")

    if not task:
        return {"success": False, "error": "task is required"}

    # Check for batch mode (list of tasks)
    tasks = args.get("tasks")
    if tasks and isinstance(tasks, list):
        return await _delegate_batch(tasks, agent_name, db, user_id, max_iterations, endpoint, context)

    # Single task
    result = await _run_sub_agent(task, agent_name, db, user_id, max_iterations, endpoint)
    return result


async def _delegate_batch(
    tasks: list[str],
    agent_name: str,
    db: Session,
    user_id: str | None,
    max_iterations: int,
    endpoint: LLMEndpoint | None = None,
    context: dict | None = None,
) -> dict:
    """Run multiple sub-agent tasks concurrently.

    When the v3 FSM passes ``context["progress_emitter"]`` (mutable list) +
    ``context["step_counter"]`` ([int]), emits one activity_step pair per
    sub-agent (running → done) so the chat UI shows parallel worker cards.
    No-op otherwise (safe for tests / batch callers).
    """
    import time as _time

    _progress = (context or {}).get("progress_emitter")
    _step_ctr = (context or {}).get("step_counter")

    def _emit(label: str, status: str, dur_ms: int | None = None, tool: str = "delegate_task") -> None:
        if _progress is None or _step_ctr is None:
            return
        _step_ctr[0] += 1
        from app.services.agent_loop.sse_builders import _emit_activity_step

        _progress.append(
            _emit_activity_step(_step_ctr[0], label, status, duration_ms=dur_ms, tool_name=tool)
        )

    starts = [_time.monotonic() for _ in tasks]
    for i, task in enumerate(tasks):
        _emit(f"Sub-agent {i + 1}: {task[:60]}", "running")

    async def _run_one(i: int, task: str) -> dict:
        try:
            res = await _run_sub_agent(task, agent_name, db, user_id, max_iterations, endpoint)
        except Exception as exc:  # noqa: BLE001
            res = {"success": False, "error": str(exc), "task": task}
        dur = int((_time.monotonic() - starts[i]) * 1000)
        _emit(
            f"Sub-agent {i + 1}: {task[:60]}",
            "done" if res.get("success") else "failed",
            dur_ms=dur,
        )
        return res

    results = await asyncio.gather(*[_run_one(i, t) for i, t in enumerate(tasks)], return_exceptions=True)

    batch_results = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            batch_results.append({
                "task": tasks[i][:100],
                "success": False,
                "error": str(r),
            })
        else:
            batch_results.append({
                "task": tasks[i][:100],
                **r,
            })

    return {
        "success": all(r.get("success", False) for r in batch_results),
        "results": batch_results,
        "count": len(batch_results),
    }


async def _run_sub_agent(
    task: str,
    agent_name: str,
    db: Session,
    user_id: str | None,
    max_iterations: int,
    endpoint: LLMEndpoint | None = None,
) -> dict:
    """Run a sub-agent conversation with restricted tools (recorded)."""
    from datetime import datetime, timezone

    from app.services.agent_invocations import record_invocation

    _started = datetime.now(timezone.utc)
    try:
        result = await _run_sub_agent_inner(
            task, agent_name, db, user_id, max_iterations, endpoint,
        )
    except Exception as exc:  # noqa: BLE001
        result = {"success": False, "error": str(exc), "task": task}
    finally:
        # Per-sub-agent invocation row (P1, 2026-08-29): duration + status so
        # delegation is observable and costed like the main loop.
        try:
            _it = result.get("iterations")
            record_invocation(
                db,
                conversation_id=None,
                user_id=user_id,
                invocation_type="sub_agent",
                input_message=task[:2000],
                status="completed" if result.get("success") else "failed",
                output_json={"task": task[:200], "iterations": _it},
                assistant_content=(result.get("response") or "")[:10000],
                error_message=result.get("error"),
                started_at=_started,
                completed_at=datetime.now(timezone.utc),
                duration_ms=max(0, int((datetime.now(timezone.utc) - _started).total_seconds() * 1000)),
                model_name=endpoint.model_id if endpoint else None,
                tool_call_count=_it if isinstance(_it, int) else None,
            )
        except Exception as _inv_err:  # noqa: BLE001 — non-fatal
            logger.warning("delegate: invocation record failed (non-fatal): %s", _inv_err)
    return result


async def _run_sub_agent_inner(
    task: str,
    agent_name: str,
    db: Session,
    user_id: str | None,
    max_iterations: int,
    endpoint: LLMEndpoint | None = None,
) -> dict:
    """Run a sub-agent conversation with restricted tools."""
    from app.services.agent_prompts import get_system_prompt, _get_all_crud_schemas

    # Build system prompt for the sub-agent
    system_prompt = get_system_prompt(agent_name) or (
        "You are a helpful AI assistant. Complete the task concisely and accurately."
    )
    # Add delegation context
    system_prompt += "\n\nYou are operating as a sub-agent with a restricted toolset. " \
                     "Complete the task efficiently and return your findings."

    # Build available tools (exclude restricted tools)
    all_tool_names = registry.list_available()
    available_names = [t for t in all_tool_names if t not in _RESTRICTED_TOOLS]
    tool_schemas = []
    for name in available_names:
        entry = registry.get_entry(name)
        if entry and entry.enabled_by_default:
            tool_schemas.append(_normalize_subagent_schema(name, entry.schema, entry.description or ""))

    # Also include CRUD tool schemas if the sub-agent needs them
    # (but exclude delegate_task which is not in CRUD anyway)
    crud_schemas = _get_all_crud_schemas()
    for name, schema in crud_schemas.items():
        if name not in _RESTRICTED_TOOLS and schema not in tool_schemas:
            tool_schemas.append(schema)

    # Build messages
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    # P0 reliability: per-turn guardrail controller + iteration budget
    guard_ctrl = ToolLoopGuardController()
    conv_budget = IterationBudget(max_total=max_iterations)

    # Run tool-calling loop
    for iteration in range(max_iterations):
        # P0: consume one iteration from the conversation-level budget
        if not conv_budget.consume():
            logger.info(
                "Sub-agent (delegate) iteration budget exhausted (%d/%d), breaking",
                conv_budget.used, conv_budget.max_total,
            )
            break

        # P1.3/P2: pre-API pruning + message sanitization
        pre_call_prep(messages)

        try:
            llm_response = await call_llm_with_reliability(
                messages, tool_schemas, endpoint=endpoint
            )
        except Exception as e:
            logger.warning("Sub-agent LLM call failed: %s", e)
            metrics.record_budget(conv_budget.used, conv_budget.max_total)
            return {"success": False, "error": f"LLM call failed: {e}", "task": task}

        content = llm_response.get("content", "")
        raw_tool_calls = llm_response.get("tool_calls", [])

        if not raw_tool_calls:
            # Final text response
            metrics.record_budget(conv_budget.used, conv_budget.max_total)
            return {
                "success": True,
                "task": task,
                "response": content,
                "iterations": iteration + 1,
            }

        # Process tool calls — parallel execution for multiple calls,
        # sequential for single (mirrors the main agent loop pattern).
        # return_exceptions=True ensures one failure doesn't cancel siblings.
        parsed_calls = []
        for tc in raw_tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            args_str = func.get("arguments", "{}")
            tool_call_id = tc.get("id", str(uuid.uuid4()))
            try:
                tool_args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                tool_args = {}
            parsed_calls.append({
                "tool_name": tool_name,
                "tool_args": tool_args,
                "args_str": args_str,
                "tool_call_id": tool_call_id,
            })

        # Execute tools with P0 guardrail before_call checks.
        from app.services.agent_tools import execute_tool
        if len(parsed_calls) == 1:
            call = parsed_calls[0]
            _gd = guard_ctrl.before_call(call["tool_name"], call["tool_args"])
            if _gd.allows_execution:
                results = [await execute_tool(
                    call["tool_name"],
                    call["tool_args"],
                    db, user_id,
                )]
            else:
                results = [json.loads(synthetic_blocked_result(_gd))]
        else:
            async def _exec_one(call):
                _gd = guard_ctrl.before_call(call["tool_name"], call["tool_args"])
                if _gd.allows_execution:
                    return await execute_tool(
                        call["tool_name"], call["tool_args"], db, user_id,
                    )
                return json.loads(synthetic_blocked_result(_gd))
            raw_results = await asyncio.gather(
                *[_exec_one(c) for c in parsed_calls],
                return_exceptions=True,
            )
            results = []
            for i, r in enumerate(raw_results):
                if isinstance(r, Exception):
                    logger.warning(
                        "Sub-agent tool '%s' raised: %s",
                        parsed_calls[i]["tool_name"], r,
                    )
                    results.append({"success": False, "error": f"{type(r).__name__}: {r}"})
                else:
                    results.append(r)

        # Build messages in original order (LLM API requires matching order).
        # P0: apply Layer 2 per-result persistence to large results.
        for call, result in zip(parsed_calls, results):
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call["tool_call_id"],
                    "type": "function",
                    "function": {
                        "name": call["tool_name"],
                        "arguments": call["args_str"],
                    },
                }],
            })
            _result_str = persist_result_str(
                call["tool_name"], result, None,
                context_window_tokens=(
                    endpoint.context_window if endpoint else None
                ),
            )
            messages.append({
                "role": "tool",
                "tool_call_id": call["tool_call_id"],
                "content": _result_str,
            })
            # P0: guardrail after_call records outcome for loop detection
            guard_ctrl.after_call(call["tool_name"], call["tool_args"], _result_str)

        # P0: Layer 3 — apply per-turn aggregate budget to this batch's results
        _batch_ids = [c["tool_call_id"] for c in parsed_calls]
        _batch_names = [c["tool_name"] for c in parsed_calls]
        apply_turn_budget_to_batch(
            messages, _batch_ids, _batch_names, None,
            context_window_tokens=(
                endpoint.context_window if endpoint else None
            ),
        )

        # P0: if guardrail controller tripped a halt, inject nudge and break
        if guard_ctrl.halt_decision:
            _hd = guard_ctrl.halt_decision
            logger.warning(
                "Guardrail halt in sub-agent (delegate): %s (tool=%s, count=%d)",
                _hd.code, _hd.tool_name, _hd.count,
            )
            metrics.record_guardrail_halt(_hd.code)
            messages.append({
                "role": "user",
                "content": (
                    f"Tool '{_hd.tool_name}' is looping (count={_hd.count}). "
                    "Stop calling it and produce your final answer now."
                ),
            })
            break

    metrics.record_budget(conv_budget.used, conv_budget.max_total)

    # Exceeded max iterations or guardrail halt — return last content
    return {
        "success": True,
        "task": task,
        "response": content or "Sub-agent completed but did not produce a final response.",
        "iterations": max_iterations,
        "note": "Reached max iterations",
    }


async def _call_sub_llm(messages: list[dict], tools: list[dict] | None) -> dict:
    """Call the LLM for a sub-agent conversation.

    .. deprecated::
        Retained for backward compatibility. New code should use
        :func:`app.services.sub_agent_reliability.call_llm_with_reliability`
        which adds prompt caching, error classification, and metrics.
    """
    return await call_llm_with_reliability(messages, tools)


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

DELEGATE_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Delegate a task to a sub-agent with isolated context. "
            "The sub-agent runs with a restricted toolset (no delegation, no memory) "
            "and returns its final response. "
            "Use for complex subtasks that need focused attention, or when you need "
            "to parallelize independent tasks.\n\n"
            "Pass 'tasks' (array of strings) instead of 'task' to run multiple "
            "tasks concurrently and get all results at once."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task description for the sub-agent to complete.",
                },
                "tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Multiple tasks to run concurrently (batch mode). Alternative to 'task'.",
                },
                "agent_name": {
                    "type": "string",
                    "description": "Agent name for the sub-agent (default 'general_assistant')",
                    "default": "general_assistant",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Max tool-calling iterations for the sub-agent (default 5)",
                    "default": 5,
                },
            },
            "required": [],
        },
    },
}

registry.register(
    name="delegate_task",
    schema=DELEGATE_TASK_SCHEMA,
    handler=_delegate_task,
    category="delegation",
    enabled_by_default=True,
    description="Delegate a task to a sub-agent with isolated context.",
)
