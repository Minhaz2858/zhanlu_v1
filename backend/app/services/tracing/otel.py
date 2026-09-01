"""OpenTelemetry tracing provider — lazy-imports the SDK, graceful fallback.

When opentelemetry is not installed, or initialization fails, the tracer
degrades to no-op behavior. Tracing is best-effort: a tracing failure must
never break an LLM call or tool execution.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.tracing.base import Span, TracingProvider
from app.services.tracing import TraceContext

logger = logging.getLogger(__name__)


class OtelSpan(Span):
    """Span backed by an OpenTelemetry span (when SDK is available)."""

    def __init__(self, name: str, attributes: dict[str, Any] | None,
                 otel_span: Any = None):
        super().__init__(name, attributes)
        self._otel_span = otel_span

    def set_attribute(self, key: str, value: Any) -> None:
        super().set_attribute(key, value)
        if self._otel_span is not None:
            try:
                self._otel_span.set_attribute(key, value)
            except Exception:
                pass  # best-effort


def _trace_status_error(description: str):
    """Helper to create an OTEL error status without importing at module level."""
    try:
        from opentelemetry.trace import Status, StatusCode
        return Status(StatusCode.ERROR, description=description)
    except Exception:
        return None


class OtelTracer(TracingProvider):
    """Tracing provider that exports spans via OpenTelemetry OTLP.

    Lazy-imports opentelemetry at construction. If the SDK is missing or
    initialization fails, _available=False and all methods become no-ops.
    """

    def __init__(self) -> None:
        self._available: bool = False
        self._tracer: Any = None
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.resources import Resource

            from app.config import settings

            resource = Resource.create({
                "service.name": getattr(settings, "OTEL_SERVICE_NAME", "zhanlu-backend"),
            })
            provider = TracerProvider(resource=resource)

            endpoint = getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
            if endpoint:
                try:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                        OTLPSpanExporter,
                    )
                    exporter = OTLPSpanExporter(endpoint=endpoint)
                    provider.add_span_processor(BatchSpanProcessor(exporter))
                except Exception as exc:
                    logger.warning("OTLP exporter not available: %s", exc)

            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer("zhanlu")
            self._available = True
        except Exception as exc:
            logger.warning(
                "OpenTelemetry not available — tracing degraded to no-op: %s", exc
            )
            self._available = False

    def start_span(self, name: str, attributes: dict[str, Any] | None = None) -> Span:
        if not self._available or self._tracer is None:
            return Span(name, attributes)

        # Merge TraceContext values into span attributes
        ctx_attrs = TraceContext.current_attributes()
        merged = {**ctx_attrs, **(attributes or {})}
        try:
            otel_span = self._tracer.start_as_current_span(name)
            otel_span.__enter__()
            for k, v in merged.items():
                if v is not None:
                    otel_span.set_attribute(k, v)
            return OtelSpan(name, merged, otel_span=otel_span)
        except Exception:
            return Span(name, merged)

    def record_llm_call(self, *, model: str, prompt_tokens: int,
                        completion_tokens: int, latency_ms: float,
                        error: str | None = None) -> None:
        if not self._available or self._tracer is None:
            return
        try:
            with self._tracer.start_as_current_span("llm_call") as span:
                span.set_attribute("model", model)
                span.set_attribute("prompt_tokens", prompt_tokens)
                span.set_attribute("completion_tokens", completion_tokens)
                span.set_attribute("latency_ms", latency_ms)
                if error:
                    span.set_attribute("error", error)
                    status = _trace_status_error(error)
                    if status is not None:
                        span.set_status(status)
                for k, v in TraceContext.current_attributes().items():
                    if v is not None:
                        span.set_attribute(k, v)
        except Exception:
            pass  # best-effort

    def record_tool_call(self, *, tool_name: str, args_hash: str,
                         duration_ms: float, success: bool,
                         error: str | None = None) -> None:
        if not self._available or self._tracer is None:
            return
        try:
            with self._tracer.start_as_current_span("tool_call") as span:
                span.set_attribute("tool_name", tool_name)
                span.set_attribute("args_hash", args_hash)
                span.set_attribute("duration_ms", duration_ms)
                span.set_attribute("success", success)
                if error:
                    span.set_attribute("error", error)
                    status = _trace_status_error(error)
                    if status is not None:
                        span.set_status(status)
                for k, v in TraceContext.current_attributes().items():
                    if v is not None:
                        span.set_attribute(k, v)
        except Exception:
            pass  # best-effort
