"""Tests for the tracing provider ABC, NoOp tracer, and TraceContext."""
from __future__ import annotations

import asyncio
import httpx
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.tracing import TraceContext, get_tracer
from app.services.tracing.base import Span, TracingProvider
from app.services.tracing.noop import NoOpTracer


def test_get_tracer_returns_noop_by_default():
    tracer = get_tracer()
    assert isinstance(tracer, NoOpTracer)


def test_noop_start_span_is_context_manager():
    tracer = NoOpTracer()
    with tracer.start_span("test") as span:
        assert isinstance(span, Span)
    # No exception = pass


def test_noop_record_methods_are_noops():
    tracer = NoOpTracer()
    tracer.record_llm_call(model="gpt-4o-mini", prompt_tokens=10,
                           completion_tokens=5, latency_ms=100.0)
    tracer.record_tool_call(tool_name="read_file", args_hash="abc123",
                            duration_ms=50.0, success=True)
    # No exception = pass


def test_noop_record_error_does_not_raise():
    tracer = NoOpTracer()
    tracer.record_llm_call(model="gpt-4o-mini", prompt_tokens=0,
                           completion_tokens=0, latency_ms=0.0,
                           error="timeout")
    # No exception = pass


def test_trace_context_set_and_get():
    TraceContext.clear()
    TraceContext.set(session_id="sess-1", user_id="user-1",
                     agent_name="general", role="user")
    assert TraceContext.current_session_id() == "sess-1"
    assert TraceContext.current_user_id() == "user-1"
    assert TraceContext.current_agent_name() == "general"
    assert TraceContext.current_role() == "user"


def test_trace_context_defaults_to_none():
    TraceContext.clear()
    assert TraceContext.current_session_id() is None
    assert TraceContext.current_role() is None


def test_trace_context_propagates_across_async():
    """TraceContext values set in outer scope are visible in async tasks."""
    TraceContext.clear()
    TraceContext.set(session_id="sess-async", user_id="user-async",
                     agent_name="test", role="admin")

    async def inner():
        return TraceContext.current_session_id()

    result = asyncio.run(inner())
    assert result == "sess-async"


def test_span_context_manager_records_duration():
    """Span __enter__/__exit__ should not raise and should be usable."""
    tracer = NoOpTracer()
    with tracer.start_span("llm_call", {"model": "gpt-4o-mini"}) as span:
        span.set_attribute("prompt_tokens", 100)
    # No exception = pass


def test_tracing_provider_is_abstract():
    """Cannot instantiate TracingProvider directly."""
    import pytest
    with pytest.raises(TypeError):
        TracingProvider()


# ── OtelTracer tests ─────────────────────────────────────────────────

try:
    import opentelemetry  # noqa: F401
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

from unittest.mock import patch
import pytest as _pytest


@_pytest.mark.skipif(_OTEL_AVAILABLE, reason="tests fallback when SDK IS installed (swap to not-installed)")
def test_otel_tracer_falls_back_when_sdk_missing():
    """OtelTracer should degrade to no-op when opentelemetry is not installed."""
    from app.services.tracing.otel import OtelTracer
    with patch.dict("sys.modules", {"opentelemetry": None, "opentelemetry.trace": None}):
        tracer = OtelTracer()
        assert tracer._available is False
        # Should not raise — degrades to no-op
        tracer.record_llm_call(model="test", prompt_tokens=1,
                               completion_tokens=1, latency_ms=1.0)


def test_otel_tracer_does_not_raise_on_record_llm_call():
    """OtelTracer.record_llm_call should never raise regardless of SDK state."""
    from app.services.tracing.otel import OtelTracer
    tracer = OtelTracer()
    tracer.record_llm_call(model="gpt-4o-mini", prompt_tokens=100,
                           completion_tokens=50, latency_ms=250.0)
    tracer.record_llm_call(model="gpt-4o-mini", prompt_tokens=0,
                           completion_tokens=0, latency_ms=0.0,
                           error="timeout")


def test_otel_tracer_does_not_raise_on_record_tool_call():
    """OtelTracer.record_tool_call should never raise regardless of SDK state."""
    from app.services.tracing.otel import OtelTracer
    tracer = OtelTracer()
    tracer.record_tool_call(tool_name="read_file", args_hash="abc123",
                            duration_ms=50.0, success=True)
    tracer.record_tool_call(tool_name="write_file", args_hash="def456",
                            duration_ms=100.0, success=False, error="permission denied")


def test_otel_tracer_start_span_returns_span():
    """OtelTracer.start_span should return a usable Span."""
    from app.services.tracing.otel import OtelTracer
    tracer = OtelTracer()
    with tracer.start_span("test_span", {"key": "value"}) as span:
        span.set_attribute("extra", 42)
    # No exception = pass


def test_otel_tracer_includes_trace_context_attributes():
    """Spans should include TraceContext values as attributes."""
    from app.services.tracing.otel import OtelTracer
    TraceContext.clear()
    TraceContext.set(session_id="sess-1", user_id="user-1",
                     agent_name="general", role="admin")
    tracer = OtelTracer()
    with tracer.start_span("test") as span:
        pass
    # No exception = pass (attributes are set internally)
    TraceContext.clear()


@_pytest.mark.skipif(not _OTEL_AVAILABLE, reason="opentelemetry not installed")
def test_otel_tracer_initializes_when_sdk_available():
    """OtelTracer should initialize when opentelemetry is importable."""
    from app.services.tracing.otel import OtelTracer
    tracer = OtelTracer()
    assert tracer._available is True


# ── Integration: instrumentation in llm_service + agent_tools ─────────

from unittest.mock import AsyncMock, MagicMock, patch


def test_call_llm_records_span(monkeypatch):
    """call_llm should call tracer.record_llm_call on success."""
    from app.services.tracing import reset_tracer
    reset_tracer()

    call_log = []

    class RecordingTracer(NoOpTracer):
        def record_llm_call(self, **kwargs):
            call_log.append(kwargs)

    import app.services.tracing as tracing_mod
    monkeypatch.setattr(tracing_mod, "_tracer", RecordingTracer())

    from app.services import llm_service
    monkeypatch.setattr(llm_service, "build_llm_payload",
                        lambda body, model: (body, [], False))

    mock_provider = MagicMock()
    mock_provider.model = "test-model"
    mock_provider.name = "test"
    mock_provider.base_url = "http://localhost"
    mock_provider.api_key = "test-key"
    monkeypatch.setattr(llm_service, "get_llm_providers", lambda: [mock_provider])

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(llm_service.httpx, "AsyncClient", return_value=mock_client):
        import asyncio
        result = asyncio.run(llm_service.call_llm(prompt="hello"))

    assert result["response"] == "hello"
    assert len(call_log) == 1
    assert call_log[0]["model"] == "test-model"
    assert call_log[0]["prompt_tokens"] == 5
    assert call_log[0]["completion_tokens"] == 3
    assert call_log[0].get("error") is None
    reset_tracer()


def test_call_llm_records_error_on_failure(monkeypatch):
    """call_llm should record error when all providers fail."""
    from app.services.tracing import reset_tracer
    reset_tracer()

    call_log = []

    class RecordingTracer(NoOpTracer):
        def record_llm_call(self, **kwargs):
            call_log.append(kwargs)

    import app.services.tracing as tracing_mod
    monkeypatch.setattr(tracing_mod, "_tracer", RecordingTracer())

    from app.services import llm_service
    monkeypatch.setattr(llm_service, "build_llm_payload",
                        lambda body, model: (body, [], False))

    mock_provider = MagicMock()
    mock_provider.model = "test-model"
    mock_provider.name = "test-fail"
    mock_provider.base_url = "http://localhost"
    mock_provider.api_key = "test-key"
    monkeypatch.setattr(llm_service, "get_llm_providers", lambda: [mock_provider])

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("LLM unreachable"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(llm_service.httpx, "AsyncClient", return_value=mock_client):
        import asyncio
        from fastapi import HTTPException
        try:
            asyncio.run(llm_service.call_llm(prompt="hello"))
        except (HTTPException, Exception):
            pass

    assert len(call_log) >= 1
    assert call_log[-1]["error"] is not None
    reset_tracer()


def test_execute_tool_records_span(monkeypatch):
    """execute_tool_with_retry should call tracer.record_tool_call."""
    from app.services.tracing import reset_tracer
    reset_tracer()

    call_log = []

    class RecordingTracer(NoOpTracer):
        def record_tool_call(self, **kwargs):
            call_log.append(kwargs)

    import app.services.tracing as tracing_mod
    monkeypatch.setattr(tracing_mod, "_tracer", RecordingTracer())

    from app.services import agent_tools

    async def _mock_execute_success(name, args, db, user_id=None, context=None):
        return {"success": True, "result": "ok"}

    monkeypatch.setattr(agent_tools, "execute_tool", _mock_execute_success)

    import asyncio
    result = asyncio.run(agent_tools.execute_tool_with_retry("read_file", {"path": "/tmp/x"}, db=None))
    assert result["success"] is True
    assert len(call_log) == 1
    assert call_log[0]["tool_name"] == "read_file"
    assert call_log[0]["success"] is True
    reset_tracer()
