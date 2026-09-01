"""Per-turn tool-call loop guardrail controller.

Complements the history-scan ``_detect_tool_call_loop`` in agents.py with
finer-grained, incremental, per-turn detection of three loop patterns:

1. **Exact-failure loop**: same tool + same args failing repeatedly.
2. **Same-tool-failure loop**: same tool failing with DIFFERENT args.
3. **No-progress loop**: idempotent (read-only) tool returning identical results.

The controller is stateful but side-effect free: it tracks per-turn
observations and returns decisions. The turn loop owns whether a "block"
decision becomes a synthetic tool result or a turn halt.

Inspired by Hermes' ``agent/tool_guardrails.py``, adapted for Zhanlu's
OpenAI-compatible message format (dicts, not dataclasses).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

# P1.2: Tool classification is the single source of truth for which tools
# have side effects. The guardrail uses it to decide idempotency.
from app.services.tool_result_classification import (
    NO_EFFECT_TOOL_NAMES,
    FILE_MUTATING_TOOL_NAMES,
    STATE_MUTATING_TOOL_NAMES,
)

logger = logging.getLogger(__name__)

# Read-only tools where identical output = no progress.
# Sourced from tool_result_classification.NO_EFFECT_TOOL_NAMES (P1.2).
IDEMPOTENT_TOOL_NAMES: frozenset[str] = NO_EFFECT_TOOL_NAMES

# Mutating tools -- repeated calls are never "no-progress" (the world may have changed).
# Sourced from tool_result_classification (P1.2).
MUTATING_TOOL_NAMES: frozenset[str] = FILE_MUTATING_TOOL_NAMES | STATE_MUTATING_TOOL_NAMES


@dataclass(frozen=True)
class ToolGuardrailConfig:
    """Thresholds for per-turn loop detection.

    Warnings are enabled by default. Hard stops are also enabled by default
    (Zhanlu's turn loop already breaks on loop detection, so hard-stop is
    the expected behavior).
    """
    warnings_enabled: bool = True
    hard_stop_enabled: bool = True
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Decision returned by the controller."""
    action: str = "allow"  # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}

    def to_metadata(self) -> dict[str, Any]:
        return {
            "action": self.action, "code": self.code,
            "message": self.message, "tool_name": self.tool_name,
            "count": self.count,
        }


def _canonical_args(args: Mapping[str, Any] | None) -> str:
    if not isinstance(args, Mapping):
        args = {}
    return json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _args_hash(args: Mapping[str, Any] | None) -> str:
    return hashlib.sha256(_canonical_args(args).encode("utf-8", "surrogatepass")).hexdigest()


def _result_hash(result: str | None) -> str:
    """Stable hash of a tool result string."""
    return hashlib.sha256((result or "").encode("utf-8", "surrogatepass")).hexdigest()


def _is_failure(result_content: str | None) -> bool:
    """Classify a tool result JSON string as failure or success.

    A result is a failure when ``success`` is explicitly ``False`` and it is
    not an approval-pause (``requires_approval``). Unknown / unparseable
    results are treated as success (conservative: don't mask real loops).
    """
    if not result_content:
        return False
    try:
        payload = json.loads(result_content)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("requires_approval"):
        return False
    return payload.get("success") is False


class ToolLoopGuardController:
    """Per-turn controller for repeated failed / non-progressing tool calls.

    Usage in a turn loop::

        ctrl = ToolLoopGuardController()
        for call in tool_calls:
            decision = ctrl.before_call(name, args)
            if not decision.allows_execution:
                # inject synthetic blocked result
                continue
            result = await execute_tool(...)
            decision = ctrl.after_call(name, args, result_json)
            if decision.should_halt:
                break
    """

    def __init__(self, config: ToolGuardrailConfig | None = None):
        self.config = config or ToolGuardrailConfig()
        self.reset_for_turn()

    def reset_for_turn(self) -> None:
        self._exact_failure_counts: dict[str, int] = {}  # key = name+args_hash
        self._same_tool_failure_counts: dict[str, int] = {}  # key = name
        self._no_progress: dict[str, tuple[str, int]] = {}  # key -> (result_hash, count)
        self._halt_decision: ToolGuardrailDecision | None = None

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        return self._halt_decision

    def _signature_key(self, tool_name: str, args: Mapping[str, Any] | None) -> str:
        return f"{tool_name}:{_args_hash(args)}"

    def _is_idempotent(self, tool_name: str) -> bool:
        if tool_name in self.config.mutating_tools:
            return False
        return tool_name in self.config.idempotent_tools

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        """Check before executing a tool. Returns block decision if a hard-stop threshold is met."""
        if not self.config.hard_stop_enabled or self._halt_decision:
            return ToolGuardrailDecision(tool_name=tool_name)

        sig_key = self._signature_key(tool_name, args)

        # Exact-failure block check
        exact_count = self._exact_failure_counts.get(sig_key, 0)
        if exact_count >= self.config.exact_failure_block_after:
            decision = ToolGuardrailDecision(
                action="block", code="repeated_exact_failure_block",
                message=(
                    f"Blocked {tool_name}: the same call failed {exact_count} times. "
                    "Stop retrying it unchanged; change strategy or explain the blocker."
                ),
                tool_name=tool_name, count=exact_count,
            )
            self._halt_decision = decision
            return decision

        # No-progress block check (idempotent only)
        if self._is_idempotent(tool_name):
            record = self._no_progress.get(sig_key)
            if record is not None and record[1] >= self.config.no_progress_block_after:
                decision = ToolGuardrailDecision(
                    action="block", code="no_progress_block",
                    message=(
                        f"Blocked {tool_name}: returned the same result {record[1]} times. "
                        "Use the result already provided or try a different query."
                    ),
                    tool_name=tool_name, count=record[1],
                )
                self._halt_decision = decision
                return decision

        return ToolGuardrailDecision(tool_name=tool_name)

    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result_content: str | None,
    ) -> ToolGuardrailDecision:
        """Record the outcome and return a warn/halt decision if a threshold is crossed."""
        if self._halt_decision:
            return self._halt_decision

        sig_key = self._signature_key(tool_name, args)
        failed = _is_failure(result_content)

        if failed:
            # Exact-failure tracking
            exact_count = self._exact_failure_counts.get(sig_key, 0) + 1
            self._exact_failure_counts[sig_key] = exact_count
            self._no_progress.pop(sig_key, None)

            # Same-tool-failure tracking (different args, same tool)
            same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_count

            # Hard stop: same tool failing too many times regardless of args
            if self.config.hard_stop_enabled and same_count >= self.config.same_tool_failure_halt_after:
                decision = ToolGuardrailDecision(
                    action="halt", code="same_tool_failure_halt",
                    message=(
                        f"Stopped {tool_name}: it failed {same_count} times this turn. "
                        "Choose a different approach."
                    ),
                    tool_name=tool_name, count=same_count,
                )
                self._halt_decision = decision
                return decision

            # Warning: exact failure repeated
            if self.config.warnings_enabled and exact_count >= self.config.exact_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn", code="repeated_exact_failure_warning",
                    message=(
                        f"{tool_name} failed {exact_count} times with identical arguments. "
                        "Inspect the error and change strategy."
                    ),
                    tool_name=tool_name, count=exact_count,
                )

            # Warning: same tool failing with different args
            if self.config.warnings_enabled and same_count >= self.config.same_tool_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn", code="same_tool_failure_warning",
                    message=(
                        f"{tool_name} failed {same_count} times this turn with different arguments. "
                        "Diagnose the root cause before retrying."
                    ),
                    tool_name=tool_name, count=same_count,
                )

            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count)

        # Success -- reset failure counters for this signature
        self._exact_failure_counts.pop(sig_key, None)
        self._same_tool_failure_counts.pop(tool_name, None)

        # No-progress tracking (idempotent only)
        if not self._is_idempotent(tool_name):
            self._no_progress.pop(sig_key, None)
            return ToolGuardrailDecision(tool_name=tool_name)

        r_hash = _result_hash(result_content)
        previous = self._no_progress.get(sig_key)
        repeat_count = 1
        if previous is not None and previous[0] == r_hash:
            repeat_count = previous[1] + 1
        self._no_progress[sig_key] = (r_hash, repeat_count)

        if self.config.warnings_enabled and repeat_count >= self.config.no_progress_warn_after:
            return ToolGuardrailDecision(
                action="warn", code="no_progress_warning",
                message=(
                    f"{tool_name} returned the same result {repeat_count} times. "
                    "Use the result already provided or change the query."
                ),
                tool_name=tool_name, count=repeat_count,
            )

        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count)


def synthetic_blocked_result(decision: ToolGuardrailDecision) -> str:
    """Build a JSON tool-result string for a blocked call."""
    return json.dumps({
        "success": False,
        "error": decision.message,
        "guardrail": decision.to_metadata(),
    }, ensure_ascii=False)
