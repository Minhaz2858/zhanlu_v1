"""Harness policy — the single source of truth for turn budgets (Phase 1a).

Before this module the turn budgets were literals scattered through an 18k-line
router (``MAX_TOOL_ITERATIONS = 40``, ``TOOL_CALL_HARD_CAP = 10``,
``_NON_ANSWER_MAX_CHARS = 600`` …). Budgets are a *harness* concern: one place,
overridable per agent class without editing the router. Bounding an autonomous
loop is correct and stays — what changes is that the VALUE is policy, not a
literal buried in a call site.

Resolution order (most specific wins):
  1. per-agent-class override  ``HARNESS_BUDGETS_<CLASS>_JSON``
     e.g. ``HARNESS_BUDGETS_GENERAL_ASSISTANT_JSON='{"max_tool_iterations": 60}'``
  2. per-field override        ``HARNESS_BUDGET_<FIELD>``
     e.g. ``HARNESS_MAX_TOOL_ITERATIONS=60``
  3. the dataclass default (= the historical literal, so behaviour is unchanged)

Invalid or non-positive values are ignored with a warning and the default is
used — a bad env var must never produce an unbounded loop.

See ``docs/plans/2026-09-18-agent-harness-migration.md`` (Phase 1a).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HarnessBudgets:
    """Defaults are the historical literals — Phase 1a is a refactor, not a change."""

    # Loop shape
    max_tool_iterations: int = 40
    tool_call_hard_cap: int = 10
    guardrail_retries: int = 2
    # Answer-quality proxies (Phase 4 replaces these with an evaluator loop)
    non_answer_max_chars: int = 600
    weak_content_max_chars: int = 160
    weak_content_synth_min_chars: int = 400
    # Inline/inspection caps
    max_inline_rows: int = 100
    max_inline_chars: int = 4000
    max_command_chars: int = 600
    max_output_chars: int = 500

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


_FIELD_NAMES = {f.name for f in fields(HarnessBudgets)}


def _class_env_suffix(agent_class: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (agent_class or "").upper()).strip("_")


def _coerce(field: str, raw: Any) -> Optional[int]:
    """Positive int or None. A bad value is refused, never guessed."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("harness policy: ignoring non-integer %s=%r", field, raw)
        return None
    if value <= 0:
        logger.warning("harness policy: ignoring non-positive %s=%r", field, raw)
        return None
    return value


def _apply_override(base: HarnessBudgets, overrides: dict[str, Any], source: str) -> HarnessBudgets:
    clean: dict[str, int] = {}
    for key, raw in (overrides or {}).items():
        if key not in _FIELD_NAMES:
            logger.warning("harness policy: unknown budget %r in %s", key, source)
            continue
        value = _coerce(key, raw)
        if value is not None:
            clean[key] = value
    return replace(base, **clean) if clean else base


class HarnessPolicy:
    """Budgets, resolvable per agent class, read from the environment on demand."""

    def __init__(self) -> None:
        self._class_cache: dict[str, HarnessBudgets] = {}

    # ── global ────────────────────────────────────────────────────────────
    def budgets(self) -> HarnessBudgets:
        base = HarnessBudgets()
        # JSON block first, then per-field — most specific wins, matching the
        # documented resolution order (a coarse block must not override an
        # explicit single-field setting).
        raw = os.getenv("HARNESS_BUDGETS_JSON")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    base = _apply_override(base, parsed, "HARNESS_BUDGETS_JSON")
                else:
                    logger.warning("harness policy: HARNESS_BUDGETS_JSON is not an object")
            except json.JSONDecodeError:
                logger.warning("harness policy: HARNESS_BUDGETS_JSON is not valid JSON")
        per_field = {
            name: os.environ[f"HARNESS_BUDGET_{name.upper()}"]
            for name in _FIELD_NAMES
            if f"HARNESS_BUDGET_{name.upper()}" in os.environ
        }
        return _apply_override(base, per_field, "per-field env")

    # ── per agent class ───────────────────────────────────────────────────
    def for_agent_class(self, agent_class: Optional[str]) -> HarnessBudgets:
        """Budgets for one agent class. Unknown/absent class → global budgets.

        A lean tool surface should not silently carry a broad agent's budget and
        vice versa; this is the seam that lets each class be tuned from config
        instead of from a code edit.
        """
        if not agent_class:
            return self.budgets()
        key = _class_env_suffix(agent_class)
        if key in self._class_cache:
            return self._class_cache[key]
        base = self.budgets()
        raw = os.getenv(f"HARNESS_BUDGETS_{key}_JSON")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    base = _apply_override(base, parsed, f"HARNESS_BUDGETS_{key}_JSON")
                else:
                    logger.warning(
                        "harness policy: HARNESS_BUDGETS_%s_JSON is not an object", key
                    )
            except json.JSONDecodeError:
                logger.warning(
                    "harness policy: HARNESS_BUDGETS_%s_JSON is not valid JSON", key
                )
        self._class_cache[key] = base
        return base

    def describe(self) -> dict[str, Any]:
        """Global budgets + every cached class override — for ops/debug endpoints."""
        return {
            "global": self.budgets().to_dict(),
            "by_agent_class": {k: v.to_dict() for k, v in self._class_cache.items()},
        }


_POLICY: Optional[HarnessPolicy] = None


def get_policy() -> HarnessPolicy:
    global _POLICY
    if _POLICY is None:
        _POLICY = HarnessPolicy()
    return _POLICY


def reset_policy() -> None:
    """Drop the singleton + class cache (tests, or after an env change)."""
    global _POLICY
    _POLICY = None
