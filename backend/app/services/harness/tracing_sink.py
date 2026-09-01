"""TracingSink — wire OTEL spans into orchestrator events.

Wraps ``get_tracer()`` so when ``TRACING_PROVIDER`` is "noop" there is
zero overhead (all calls are no-ops).  When ``TRACING_PROVIDER="otel"``
the sink opens a run-level span and records per-iteration LLM-call and
tool-call metrics.

Implementation note:
- ``Span`` is a synchronised context manager — we ``__enter__`` on the
  first ``run_start`` event and ``__exit__`` on the final ``run_finish``.
- ``record_llm_call`` / ``record_tool_call`` are fire-and-forget best-
  effort calls (the provider swallows internal errors).
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.tracing import get_tracer, Span

logger = logging.getLogger(__name__)


class TracingSink:
    """Observability sink that bridges orchestrator events → OTEL.

    Drop-in ``EventSink`` compatible callable.  Construct once and pass
    as ``event_sink`` to ``AgentRunOrchestrator``.  The sink is a
    no-op when ``TRACING_PROVIDER`` is "noop" (default).
    """

    def __init__(self, *, enabled: bool = True):
        self._enabled = enabled
        self._tracer = get_tracer()
        self._run_span: Span | None = None

    def __call__(self, event: dict[str, Any]) -> None:
        if not self._enabled:
            return
        try:
            self._handle(event)
        except Exception as e:
            logger.debug("TracingSink: event handling failed (%s): %s",
                         event.get("type", "?"), e)

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _handle(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")

        if etype == "run_start":
            self._on_run_start(event)
        elif etype == "run_finish":
            self._on_run_finish(event)
        elif etype == "llm_call":
            self._on_llm_call(event)
        elif etype == "tool_call":
            self._on_tool_call(event)

    def _on_run_start(self, event: dict[str, Any]) -> None:
        attrs = {
            "agent_name": event.get("agent_name", ""),
            "run_id": event.get("run_id", ""),
            "max_iterations": event.get("max_iterations", 0),
        }
        self._run_span = self._tracer.start_span("agent_run", attrs)
        self._run_span.__enter__()

    def _on_run_finish(self, event: dict[str, Any]) -> None:
        if self._run_span is None:
            return
        for key in ("iterations", "tool_call_count", "success", "error"):
            val = event.get(key)
            if val is not None:
                self._run_span.set_attribute(key, val)
        self._run_span.__exit__(None, None, None)
        self._run_span = None

    def _on_llm_call(self, event: dict[str, Any]) -> None:
        # Open a short iteration span + record the LLM metrics
        iter_span = self._tracer.start_span(
            "iteration",
            {
                "run_id": event.get("run_id", ""),
                "iteration": event.get("iteration", 0),
                "tool_call_count": event.get("tool_call_count", 0),
            },
        )
        iter_span.__enter__()
        try:
            self._tracer.record_llm_call(
                model=event.get("model", "default"),
                prompt_tokens=event.get("prompt_tokens", 0),
                completion_tokens=event.get("completion_tokens", 0),
                latency_ms=event.get("duration_ms", 0.0),
                error=event.get("error"),
            )
        finally:
            iter_span.__exit__(None, None, None)

    def _on_tool_call(self, event: dict[str, Any]) -> None:
        self._tracer.record_tool_call(
            tool_name=event.get("tool_name", ""),
            args_hash=event.get("args_hash", ""),
            duration_ms=event.get("duration_ms", 0.0),
            success=bool(event.get("success", True)),
            error=event.get("error"),
        )
