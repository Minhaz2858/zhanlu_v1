"""Reward function — formalizes per-turn scalar rewards (RL Phase 0).

Every agent invocation gets ONE reward scalar with auditable components:

    reward = outcome + latency_penalty + tool_efficiency + quality_bonus

- outcome:          +1.0 completed / −1.0 failed / −0.5 timeout-cancelled
- latency_penalty:  −min(1, duration_s / 60) × 0.5  (60s → −0.5)
- tool_efficiency:  −0.05 × max(0, tool_calls − 8), capped at −0.5
                    (speculative iteration is penalized, useful tools are not)
- quality_bonus:    +0.5 × judge completeness when an EvalResult/verification
                    score is supplied by the caller (optional)

Weights live here in one place — tuning them is a code review, not a hunt.
Fail-safe: reward computation never raises into the recording path.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SLOW_TURN_SECONDS = 60.0
_TOOL_BASELINE = 8
_TOOL_PENALTY_PER_EXTRA = 0.05
_TOOL_PENALTY_CAP = 0.5
_QUALITY_BONUS_WEIGHT = 0.5

OUTCOME_REWARDS = {
    "completed": 1.0,
    "failed": -1.0,
    "timeout": -0.5,
    "cancelled": -0.5,
    "pending": 0.0,
    "running": 0.0,
}


def compute_reward(
    *,
    status: str | None,
    duration_ms: int | None = None,
    tool_call_count: int | None = None,
    quality_score: float | None = None,
) -> tuple[float, dict[str, Any]]:
    """Return ``(reward, components)`` for one invocation."""
    components: dict[str, Any] = {}

    outcome = OUTCOME_REWARDS.get((status or "").lower(), 0.0)
    components["outcome"] = round(outcome, 3)

    latency_penalty = 0.0
    if duration_ms is not None and duration_ms > 0:
        seconds = duration_ms / 1000.0
        latency_penalty = -min(1.0, seconds / _SLOW_TURN_SECONDS) * 0.5
    components["latency_penalty"] = round(latency_penalty, 3)

    tool_efficiency = 0.0
    if isinstance(tool_call_count, int) and tool_call_count > _TOOL_BASELINE:
        tool_efficiency = -min(
            _TOOL_PENALTY_CAP,
            (tool_call_count - _TOOL_BASELINE) * _TOOL_PENALTY_PER_EXTRA,
        )
    components["tool_efficiency"] = round(tool_efficiency, 3)

    quality_bonus = 0.0
    if isinstance(quality_score, (int, float)):
        # Judge scores are 0-1 by contract — clamp so a mis-scaled judge
        # (0-100) can't inject an unbounded bonus into the reward.
        clamped = max(0.0, min(1.0, float(quality_score)))
        quality_bonus = _QUALITY_BONUS_WEIGHT * clamped
        components["quality_bonus"] = round(quality_bonus, 3)

    reward = outcome + latency_penalty + tool_efficiency + quality_bonus
    components["total"] = round(reward, 3)
    # Full-precision total: tests assert proportional penalties with
    # pytest.approx (rel=1e-6); rounding here breaks them.
    return reward, components


__all__ = ["compute_reward", "OUTCOME_REWARDS"]
