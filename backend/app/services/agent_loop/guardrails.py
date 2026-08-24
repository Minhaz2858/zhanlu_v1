"""Batch-level guardrail helpers for the agent loop (P2-12 extraction).

Builds on ``app.services.tool_loop_guardrails.ToolLoopGuardController``
(the per-call guard engine) with pure, unit-testable policy helpers:

- ``apply_guardrails`` — partition a parsed tool-call batch into
  executable calls and guard-blocked synthetic results.
- ``enforce_tool_caps`` — per-tool call-cap check (same semantics as the
  router's ``TOOL_CALL_CAPS`` / hard-cap logic).
- ``maybe_force_finish_line`` — final-iteration ``tool_choice="none"``
  override (extracted verbatim from ``_finish_line_tool_choice``).
- ``maybe_wrap_up_nudge`` — T-minus-N wrap-up nudge text policy.
- ``pause_for_approval`` — human-approval pause record builder.

The agents router re-imports ``maybe_force_finish_line`` into its own
namespace so existing call sites keep working unchanged.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def maybe_force_finish_line(
    iteration: int,
    final_iteration: int,
    dashboard_forced: bool,
    tool_choice: dict | None,
) -> dict | str | None:
    """Final-iteration override: force ``tool_choice="none"`` so the LLM must
    produce a text answer instead of issuing yet another tool call.

    This is the "guaranteed final answer" finish line. When the loop reaches
    its last iteration, the LLM has gathered everything it is going to gather;
    telling it ``tool_choice="none"`` forces it to synthesize a prose answer
    from whatever tool results are already in the message history, instead of
    one more exploratory call that would exhaust the budget and dump us into
    the generic empty-content fallback.

    Precedence (highest first):

      1. Dashboard-guard forcing — calling ``create_dashboard`` IS the finish
         line for dashboard requests; it always wins over ``"none"`` so a
         dashboard turn ends by producing the artifact, not prose.
      2. Existing ``tool_choice`` — a forced tool (``ask_data_agent`` /
         ``create_artifact`` / ``web_extract`` / ``web_search``) computed for
         this iteration is preserved on non-final iterations.
      3. Forced ``"none"`` — only on the FINAL iteration, and only when the
         dashboard guard did not already force ``create_dashboard``.

    Args:
        iteration: Current (0-based) loop variable.
        final_iteration: Last value the loop variable will take (exclusive
            bound minus one).
        dashboard_forced: True when the dashboard guard forced
            ``create_dashboard`` this iteration.
        tool_choice: The ``tool_choice`` computed so far this iteration
            (``None`` means "auto", a dict means a forced function).

    Returns:
        ``"none"`` on the final iteration (unless dashboard-forced), else the
        incoming ``tool_choice`` unchanged.
    """
    if iteration >= final_iteration and not dashboard_forced:
        return "none"
    return tool_choice


def apply_guardrails(
    parsed_calls: list,
    *,
    before_call,
    blocked_result_factory=None,
) -> tuple[list, list]:
    """Partition a parsed tool-call batch by the per-call guard.

    Args:
        parsed_calls: list of ``{"tool_name", "args", ...}`` dicts.
        before_call: sync callable ``(tool_name, args) -> decision`` where
            ``decision.allows_execution`` gates execution.
        blocked_result_factory: optional callable ``(decision) -> str``
            producing a serialized synthetic result for blocked calls.

    Returns:
        ``(executable_calls, blocked_results)`` — the calls that pass the
        guard, and the deserialized synthetic results for blocked calls
        (order-preserving for both lists).
    """
    executable: list = []
    blocked: list = []
    for call in parsed_calls:
        gd = before_call(call["tool_name"], call["args"])
        if gd.allows_execution:
            executable.append(call)
        elif blocked_result_factory is not None:
            blocked.append(json.loads(blocked_result_factory(gd)))
        else:
            blocked.append({"success": False, "error": "blocked by guardrail"})
    return executable, blocked


def enforce_tool_caps(
    tool_name: str,
    executed_count: int,
    failed_count: int = 0,
    caps: dict | None = None,
    dynamic_caps: dict | None = None,
    hard_cap: int | None = None,
) -> bool:
    """True when ``tool_name`` has hit its per-turn call cap.

    Same resolution order as the router: ``dynamic_caps`` (turn-computed)
    wins over static ``caps``, which wins over ``hard_cap``. A call is
    blocked when the executed count reaches the cap, or when failures
    exceed the cap (failing tools burn budget faster).
    """
    cap = (dynamic_caps or {}).get(tool_name, (caps or {}).get(tool_name, hard_cap))
    if cap is None:
        return False
    return executed_count >= cap or failed_count >= cap + 1


def maybe_wrap_up_nudge(
    iteration: int,
    final_iteration: int,
    margin: int = 3,
    already_nudged: bool = False,
) -> str | None:
    """Return a wrap-up nudge when the loop is within ``margin`` iterations of
    the final iteration and the nudge has not been sent yet this turn.

    The router injects this as an extra system/user turn (with
    ``tool_choice`` unchanged) so the model starts wrapping up before the
    hard finish line, avoiding a truncated final message.
    """
    if already_nudged:
        return None
    if iteration >= final_iteration - margin:
        return (
            "You are approaching the end of your tool budget. Wrap up now: "
            "synthesize your final answer from the tool results you already "
            "have. Do not start new explorations unless strictly required."
        )
    return None


def pause_for_approval(
    result: dict,
    call: dict,
    display_name: str,
    remaining_calls: list | None = None,
) -> tuple[dict, dict]:
    """Build the ``awaiting_approval`` frontend record + pending-tool payload
    when a tool result requires human approval.

    Args:
        result: the tool result dict containing ``requires_approval``.
        call: the parsed call dict (``tool_name`` / ``args`` / ``args_str`` /
            ``tool_call_id``).
        display_name: human-facing tool name for the frontend chip.
        remaining_calls: calls after ``call`` in the batch that should be
            deferred until approval is granted.

    Returns:
        ``(tool_call_record, pending_tool)`` — the record appended to
        ``tool_calls_for_frontend`` and the pending-tool payload handed to
        the approval flow. When ``result`` does not request approval,
        returns ``(None, None)``.
    """
    if not (isinstance(result, dict) and result.get("requires_approval")):
        return None, None
    approval_id = result.get("approval_id") or call.get("approval_id")
    tool_call_record = {
        "id": call.get("tool_call_id", ""),
        "name": display_name,
        "arguments_string": call.get("args_str", ""),
        "results": result,
        "status": "awaiting_approval",
        "approval_id": approval_id,
        "reason": result.get("reason", ""),
    }
    pending_tool = {
        "tool_name": call.get("tool_name"),
        "args": call.get("args"),
        "args_str": call.get("args_str", ""),
        "tool_call_id": call.get("tool_call_id", ""),
        "approval_id": approval_id,
        "remaining_calls": list(remaining_calls or []),
    }
    return tool_call_record, pending_tool


__all__ = [
    "maybe_force_finish_line",
    "apply_guardrails",
    "enforce_tool_caps",
    "maybe_wrap_up_nudge",
    "pause_for_approval",
]
