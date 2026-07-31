"""No-op tracer — default provider, zero overhead."""
from __future__ import annotations

from app.services.tracing.base import Span, TracingProvider


class NoOpTracer(TracingProvider):
    """Tracing provider that does nothing. Used when tracing is disabled."""

    def start_span(self, name: str, attributes: dict | None = None) -> Span:
        return Span(name, attributes)

    def record_llm_call(self, *, model, prompt_tokens, completion_tokens,
                        latency_ms, error=None) -> None:
        pass

    def record_tool_call(self, *, tool_name, args_hash, duration_ms,
                         success, error=None) -> None:
        pass
