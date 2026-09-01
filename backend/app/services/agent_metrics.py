"""Lightweight in-process metrics for agent reliability features.

Tracks counters and histograms for the P0-P3 reliability modules:
- Guardrail fires (by pattern: exact_failure, same_tool_failure, no_progress)
- Iteration budget consumption (used / max per conversation)
- Tool result persistence (chars saved by Layer 2 / Layer 3)
- Pre-API pruning (items pruned, chars reclaimed)
- Error classification distribution (by FailoverReason)
- Prompt cache hits / misses
- Verification-on-stop nudges fired

Thread-safe, no external dependencies (no Prometheus/Redis). Exposed via
a simple ``get_metrics_snapshot()`` for the metrics API endpoint.

Inspired by Hermes' observability patterns, adapted for Zhanlu's
FastAPI async architecture.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Counter:
    """A simple monotonic counter."""
    _value: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, n: int = 1) -> None:
        with self._lock:
            self._value += n

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


@dataclass
class Histogram:
    """A simple histogram tracking sum, count, min, max."""
    _sum: float = 0.0
    _count: int = 0
    _min: float = float("inf")
    _max: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, value: float) -> None:
        with self._lock:
            self._sum += value
            self._count += 1
            if value < self._min:
                self._min = value
            if value > self._max:
                self._max = value

    @property
    def avg(self) -> float:
        with self._lock:
            return self._sum / self._count if self._count > 0 else 0.0

    def to_dict(self) -> dict[str, float]:
        with self._lock:
            return {
                "sum": self._sum,
                "count": self._count,
                "min": self._min if self._count > 0 else 0.0,
                "max": self._max,
                "avg": self._sum / self._count if self._count > 0 else 0.0,
            }


class AgentMetrics:
    """Centralized metrics registry for agent reliability features.

    All methods are thread-safe. Use the module-level ``metrics`` singleton.
    """

    def __init__(self) -> None:
        self._start_time = time.time()

        # Guardrail counters
        self.guardrail_warnings = defaultdict(Counter)
        self.guardrail_halts = defaultdict(Counter)

        # Iteration budget
        self.budget_consumed = Histogram()
        self.budget_exhausted = Counter()

        # Tool result persistence
        self.persistence_layer2_chars_saved = Histogram()
        self.persistence_layer3_chars_saved = Histogram()

        # Pre-API pruning
        self.prune_items_pruned = Histogram()
        self.prune_chars_reclaimed = Histogram()

        # Error classification
        self.error_classification = defaultdict(Counter)

        # Prompt caching
        self.cache_enabled = Counter()
        self.cache_markers_applied = Histogram()

        # Verification-on-stop
        self.verify_nudge_fired = Counter()
        self.verify_nudge_suppressed = Counter()

        # Provider fallback
        self.fallback_triggered = Counter()
        self.fallback_succeeded = Counter()
        self.fallback_failed = Counter()

        # Message sanitization
        self.sanitize_surrogates_replaced = Counter()
        self.sanitize_args_repaired = Counter()
        self.sanitize_tool_sequence_closed = Counter()

        # Background review
        self.background_review_spawned = Counter()
        self.background_review_completed = Counter()
        self.background_review_failed = Counter()

    def record_guardrail_warning(self, code: str) -> None:
        self.guardrail_warnings[code].inc()

    def record_guardrail_halt(self, code: str) -> None:
        self.guardrail_halts[code].inc()

    def record_budget(self, used: int, max_total: int) -> None:
        self.budget_consumed.observe(used)
        if used >= max_total:
            self.budget_exhausted.inc()

    def record_persistence(self, layer: int, chars_saved: int) -> None:
        if layer == 2:
            self.persistence_layer2_chars_saved.observe(chars_saved)
        elif layer == 3:
            self.persistence_layer3_chars_saved.observe(chars_saved)

    def record_prune(self, items: int, chars: int) -> None:
        self.prune_items_pruned.observe(items)
        self.prune_chars_reclaimed.observe(chars)

    def record_error(self, reason: str) -> None:
        self.error_classification[reason].inc()

    def record_cache_markers(self, count: int) -> None:
        self.cache_markers_applied.observe(count)

    def record_verify_nudge(self, fired: bool) -> None:
        if fired:
            self.verify_nudge_fired.inc()
        else:
            self.verify_nudge_suppressed.inc()

    def record_fallback(self, succeeded: bool | None = None) -> None:
        if succeeded is None:
            self.fallback_triggered.inc()
        elif succeeded:
            self.fallback_succeeded.inc()
        else:
            self.fallback_failed.inc()

    def record_sanitize(self, surrogates: int = 0, args_repaired: int = 0, sequences_closed: int = 0) -> None:
        if surrogates:
            self.sanitize_surrogates_replaced.inc(surrogates)
        if args_repaired:
            self.sanitize_args_repaired.inc(args_repaired)
        if sequences_closed:
            self.sanitize_tool_sequence_closed.inc(sequences_closed)

    def record_background_review(self, status: str) -> None:
        if status == "spawned":
            self.background_review_spawned.inc()
        elif status == "completed":
            self.background_review_completed.inc()
        elif status == "failed":
            self.background_review_failed.inc()

    def get_snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of all metrics."""
        uptime = time.time() - self._start_time
        return {
            "uptime_seconds": round(uptime, 1),
            "guardrails": {
                "warnings": {k: v.value for k, v in self.guardrail_warnings.items()},
                "halts": {k: v.value for k, v in self.guardrail_halts.items()},
            },
            "iteration_budget": {
                "consumed": self.budget_consumed.to_dict(),
                "exhausted_count": self.budget_exhausted.value,
            },
            "result_persistence": {
                "layer2_chars_saved": self.persistence_layer2_chars_saved.to_dict(),
                "layer3_chars_saved": self.persistence_layer3_chars_saved.to_dict(),
            },
            "pre_api_prune": {
                "items_pruned": self.prune_items_pruned.to_dict(),
                "chars_reclaimed": self.prune_chars_reclaimed.to_dict(),
            },
            "error_classification": {
                k: v.value for k, v in self.error_classification.items()
            },
            "prompt_caching": {
                "enabled_count": self.cache_enabled.value,
                "markers_applied": self.cache_markers_applied.to_dict(),
            },
            "verification_on_stop": {
                "nudge_fired": self.verify_nudge_fired.value,
                "nudge_suppressed": self.verify_nudge_suppressed.value,
            },
            "provider_fallback": {
                "triggered": self.fallback_triggered.value,
                "succeeded": self.fallback_succeeded.value,
                "failed": self.fallback_failed.value,
            },
            "message_sanitization": {
                "surrogates_replaced": self.sanitize_surrogates_replaced.value,
                "args_repaired": self.sanitize_args_repaired.value,
                "tool_sequences_closed": self.sanitize_tool_sequence_closed.value,
            },
            "background_review": {
                "spawned": self.background_review_spawned.value,
                "completed": self.background_review_completed.value,
                "failed": self.background_review_failed.value,
            },
        }

    def reset(self) -> None:
        """Reset all metrics (for testing)."""
        self.__init__()


# Module-level singleton
metrics = AgentMetrics()


__all__ = ["AgentMetrics", "Counter", "Histogram", "metrics"]
