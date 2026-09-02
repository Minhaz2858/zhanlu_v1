"""In-process metrics rollup for the Admin observability dashboard.

Aggregates LLM cost/latency, tool latency/errors, FSM transitions and
workflow-node stats in memory (thread-safe). Enabled by
``TRACE_METRICS_ENABLED``; when disabled every recorder is a no-op so the
overhead is one attribute read.

This is the data source for ``GET /api/admin/traces/metrics`` — the dashboard
never queries the OTLP collector directly.
"""
from __future__ import annotations

import threading
from typing import Any

from app.config import settings


def metrics_enabled() -> bool:
    return bool(getattr(settings, "TRACE_METRICS_ENABLED", False))


class TraceMetrics:
    """Thread-safe aggregator of trace-derived metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._llm: dict[str, dict[str, float]] = {}
        self._tools: dict[str, dict[str, float]] = {}
        self._fsm_states: dict[str, int] = {}
        self._fsm_runs: dict[str, int] = {"total": 0, "errors": 0}
        self._workflow: dict[str, dict[str, float]] = {}

    # ── recorders (no-op when disabled) ─────────────────────────────────
    def record_llm(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        error: str | None = None,
    ) -> None:
        if not metrics_enabled():
            return
        key = model or "unknown"
        with self._lock:
            agg = self._llm.setdefault(key, {
                "calls": 0.0, "errors": 0.0, "prompt_tokens": 0.0,
                "completion_tokens": 0.0, "cost_usd": 0.0,
                "total_latency_ms": 0.0,
            })
            agg["calls"] += 1
            agg["prompt_tokens"] += prompt_tokens
            agg["completion_tokens"] += completion_tokens
            agg["total_latency_ms"] += latency_ms
            if error:
                agg["errors"] += 1
            from app.services.token_tracker import get_pricing

            pricing = get_pricing(key)
            agg["cost_usd"] += (
                prompt_tokens / 1000 * pricing["input"]
                + completion_tokens / 1000 * pricing["output"]
            )

    def record_tool(
        self,
        *,
        tool_name: str,
        duration_ms: float,
        success: bool,
        error: str | None = None,
    ) -> None:
        if not metrics_enabled():
            return
        with self._lock:
            agg = self._tools.setdefault(tool_name, {
                "calls": 0.0, "errors": 0.0, "total_latency_ms": 0.0,
            })
            agg["calls"] += 1
            agg["total_latency_ms"] += duration_ms
            if not success or error:
                agg["errors"] += 1

    def record_fsm_transition(self, state: str) -> None:
        if not metrics_enabled():
            return
        with self._lock:
            self._fsm_states[state] = self._fsm_states.get(state, 0) + 1

    def record_fsm_run(self, *, error: str | None = None) -> None:
        if not metrics_enabled():
            return
        with self._lock:
            self._fsm_runs["total"] += 1
            if error:
                self._fsm_runs["errors"] += 1

    def record_workflow_node(
        self,
        *,
        node_type: str,
        duration_ms: float,
        success: bool,
    ) -> None:
        if not metrics_enabled():
            return
        with self._lock:
            agg = self._workflow.setdefault(node_type, {
                "calls": 0.0, "errors": 0.0, "total_latency_ms": 0.0,
            })
            agg["calls"] += 1
            agg["total_latency_ms"] += duration_ms
            if not success:
                agg["errors"] += 1

    # ── snapshot ────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "llm": {k: dict(v) for k, v in self._llm.items()},
                "tools": {k: dict(v) for k, v in self._tools.items()},
                "fsm_states": dict(self._fsm_states),
                "fsm_runs": dict(self._fsm_runs),
                "workflow": {k: dict(v) for k, v in self._workflow.items()},
            }

    def reset(self) -> None:
        with self._lock:
            self._llm.clear()
            self._tools.clear()
            self._fsm_states.clear()
            self._fsm_runs = {"total": 0, "errors": 0}
            self._workflow.clear()


_metrics = TraceMetrics()


def get_metrics() -> TraceMetrics:
    """Singleton metrics rollup (in-process)."""
    return _metrics


__all__ = ["TraceMetrics", "get_metrics", "metrics_enabled"]
