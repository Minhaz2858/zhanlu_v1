"""Per-conversation iteration budget -- thread-safe consume/refund counter.

Bounds the TOTAL number of tool-loop iterations across all turns in a
conversation (including resumed conversations after approval pauses).
This complements the per-turn ``MAX_TOOL_ITERATIONS`` cap: a single turn
is bounded by the for-loop range, while the conversation-level budget
prevents a long multi-turn session from running indefinitely.

``execute_code`` iterations are refunded via :meth:`refund` so
programmatic tool-calling doesn't eat the budget.

Inspired by Hermes' ``agent/iteration_budget.py``.
"""
from __future__ import annotations

import threading


class IterationBudget:
    """Thread-safe iteration counter for a conversation.

    Args:
        max_total: Maximum total iterations allowed.
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration. Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (e.g. for execute_code turns)."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)


__all__ = ["IterationBudget"]
