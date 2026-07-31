"""Tracing package — pluggable observability for LLM/tool calls.

Default provider is NoOpTracer (zero overhead). Set TRACING_PROVIDER="otel"
and OTEL_EXPORTER_OTLP_ENDPOINT to enable OpenTelemetry export.
"""
from __future__ import annotations

import contextvars
from typing import Any

from app.services.tracing.base import Span, TracingProvider
from app.services.tracing.noop import NoOpTracer

# ---------------------------------------------------------------------------
# TraceContext — contextvars that flow through async calls
# ---------------------------------------------------------------------------

_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_id", default=None)
_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_id", default=None)
_agent_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_name", default=None)
_role: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "role", default=None)


class TraceContext:
    """Request-scoped tracing context carried via contextvars.

    Set once at request entry; inherited by all nested spans and async tasks.
    """

    @staticmethod
    def set(*, session_id: str | None = None, user_id: str | None = None,
            agent_name: str | None = None, role: str | None = None) -> None:
        if session_id is not None:
            _session_id.set(session_id)
        if user_id is not None:
            _user_id.set(user_id)
        if agent_name is not None:
            _agent_name.set(agent_name)
        if role is not None:
            _role.set(role)

    @staticmethod
    def clear() -> None:
        _session_id.set(None)
        _user_id.set(None)
        _agent_name.set(None)
        _role.set(None)

    @staticmethod
    def current_session_id() -> str | None:
        return _session_id.get()

    @staticmethod
    def current_user_id() -> str | None:
        return _user_id.get()

    @staticmethod
    def current_agent_name() -> str | None:
        return _agent_name.get()

    @staticmethod
    def current_role() -> str | None:
        return _role.get()

    @staticmethod
    def current_attributes() -> dict[str, Any]:
        """Return all current context values as a dict (for span attributes)."""
        return {
            "session_id": _session_id.get(),
            "user_id": _user_id.get(),
            "agent_name": _agent_name.get(),
            "role": _role.get(),
        }


# ---------------------------------------------------------------------------
# Singleton tracer factory
# ---------------------------------------------------------------------------

_tracer: TracingProvider | None = None


def get_tracer() -> TracingProvider:
    """Get the singleton TracingProvider instance.

    Reads settings.TRACING_PROVIDER:
    - "noop" (default) → NoOpTracer (zero overhead)
    - "otel" → OtelTracer (lazy-imports opentelemetry, falls back to NoOp on failure)
    """
    global _tracer
    if _tracer is not None:
        return _tracer

    from app.config import settings
    provider = getattr(settings, "TRACING_PROVIDER", "noop")

    if provider == "otel":
        try:
            from app.services.tracing.otel import OtelTracer
            _tracer = OtelTracer()
        except Exception:
            # Graceful fallback — never crash the app due to tracing
            _tracer = NoOpTracer()
    else:
        _tracer = NoOpTracer()

    return _tracer


def reset_tracer() -> None:
    """Reset the singleton (for testing)."""
    global _tracer
    _tracer = None


__all__ = [
    "TracingProvider",
    "Span",
    "NoOpTracer",
    "TraceContext",
    "get_tracer",
    "reset_tracer",
]
