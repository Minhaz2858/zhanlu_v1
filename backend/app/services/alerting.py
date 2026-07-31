"""Alerting — threshold-based alerts for agent reliability metrics.

Checks the metrics snapshot against configurable thresholds and returns
human-readable alert messages when thresholds are exceeded.

Alerts are logged as warnings by the scheduled task (every 5 minutes).
They are advisory — they don't block or modify behavior, just surface
concerning patterns to operators.

Alert types:
- Guardrail fire rate (too many loops detected)
- Budget exhaustion rate (too many conversations hitting the cap)
- Error rate (too many API errors)
- Fallback failure rate (fallbacks triggered but failing)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.services.agent_metrics import metrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertThreshold:
    """A threshold that triggers an alert when exceeded."""
    name: str
    description: str
    metric_path: str  # dot-separated path into the metrics snapshot
    threshold: float
    comparison: str = ">"  # > or >= or "=="

    def check(self, snapshot: dict[str, Any]) -> str | None:
        """Return an alert message if the threshold is exceeded, else None."""
        value: Any = snapshot
        for key in self.metric_path.split("."):
            if not isinstance(value, dict) or key not in value:
                return None
            value = value[key]

        if not isinstance(value, (int, float)):
            return None

        exceeded = False
        if self.comparison == ">":
            exceeded = value > self.threshold
        elif self.comparison == ">=":
            exceeded = value >= self.threshold
        elif self.comparison == "==":
            exceeded = value == self.threshold

        if exceeded:
            return (
                f"[{self.name}] {self.description}: "
                f"current={value} threshold={self.threshold}"
            )
        return None


# Default alert thresholds
DEFAULT_THRESHOLDS: list[AlertThreshold] = [
    AlertThreshold(
        name="guardrail_halt_rate",
        description="Guardrail halts are firing frequently — agents may be stuck in loops",
        metric_path="guardrails.halts.same_tool_failure_halt",
        threshold=10,
    ),
    AlertThreshold(
        name="no_progress_rate",
        description="No-progress blocks are frequent — agents may be repeating the same read-only calls",
        metric_path="guardrails.halts.no_progress_block",
        threshold=10,
    ),
    AlertThreshold(
        name="budget_exhaustion",
        description="Iteration budget exhaustion is high — agents may be running too long",
        metric_path="iteration_budget.exhausted_count",
        threshold=20,
    ),
    AlertThreshold(
        name="context_overflow_rate",
        description="Context overflow errors are frequent — conversations may be too long",
        metric_path="error_classification.context_overflow",
        threshold=10,
    ),
    AlertThreshold(
        name="rate_limit_rate",
        description="Rate limit errors are frequent — consider reducing request frequency or increasing quota",
        metric_path="error_classification.rate_limit",
        threshold=20,
    ),
    AlertThreshold(
        name="fallback_failure",
        description="Provider fallback failures — fallback models may also be unavailable",
        metric_path="provider_fallback.failed",
        threshold=5,
    ),
    AlertThreshold(
        name="sanitization_arg_repairs",
        description="Tool call argument repairs are frequent — model may be producing malformed JSON",
        metric_path="message_sanitization.args_repaired",
        threshold=50,
    ),
]


def check_alerts(
    thresholds: list[AlertThreshold] | None = None,
) -> list[str]:
    """Check all alert thresholds against the current metrics snapshot.

    Args:
        thresholds: Custom thresholds. Uses DEFAULT_THRESHOLDS if None.

    Returns:
        List of alert message strings (empty if no alerts).
    """
    snapshot = metrics.get_snapshot()
    alerts: list[str] = []

    for threshold in (thresholds or DEFAULT_THRESHOLDS):
        msg = threshold.check(snapshot)
        if msg:
            alerts.append(msg)

    return alerts


__all__ = [
    "AlertThreshold",
    "DEFAULT_THRESHOLDS",
    "check_alerts",
]
