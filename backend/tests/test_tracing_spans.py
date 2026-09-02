"""Tests for GenAI tracing spans + metrics rollup (Phase 1: OTel end-to-end).

Covers:
  - GenAI semantic-convention attributes on LLM spans (gen_ai.*)
  - error capture on exceptions
  - tool span success/error attributes + args-preview gating
  - FSM run/state + workflow-node spans feeding the metrics rollup
  - sampling rate and metrics aggregation for the Admin dashboard
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import pytest

from app.services.tracing.base import Span, TracingProvider
from app.services.tracing import get_tracer, reset_tracer, set_tracer


class RecordingSpan(Span):
    def __init__(self, name, attributes=None):
        super().__init__(name, attributes)
        self.exited = False

    def __exit__(self, *exc_info):
        self.exited = True
        return super().__exit__(*exc_info)


class RecordingTracer(TracingProvider):
    def __init__(self):
        self.spans: list[RecordingSpan] = []

    def start_span(self, name, attributes=None):
        s = RecordingSpan(name, attributes)
        self.spans.append(s)
        return s

    def record_llm_call(self, **kwargs):
        pass

    def record_tool_call(self, **kwargs):
        pass


@pytest.fixture(autouse=True)
def _tracer():
    reset_tracer()
    rec = RecordingTracer()
    set_tracer(rec)
    yield rec
    reset_tracer()


def _settings():
    from app.config import settings
    return settings


def test_llm_completion_span_records_genai_attributes():
    from app.services.tracing.spans import llm_completion_span, note_llm_usage

    with llm_completion_span("deepseek-v4", temperature=0.3) as span:
        note_llm_usage(model="deepseek-v4", prompt_tokens=100, completion_tokens=50)

    assert span.attributes["gen_ai.operation.name"] == "chat"
    assert span.attributes["gen_ai.request.model"] == "deepseek-v4"
    assert span.attributes["gen_ai.request.temperature"] == 0.3
    assert span.attributes["gen_ai.usage.input_tokens"] == 100
    assert span.attributes["gen_ai.usage.output_tokens"] == 50
    assert "duration_ms" in span.attributes


def test_llm_completion_span_records_error_on_exception():
    from app.services.tracing.spans import llm_completion_span

    with pytest.raises(RuntimeError, match="boom"):
        with llm_completion_span("m") as span:
            raise RuntimeError("boom")

    assert "error" in span.attributes


def test_traced_llm_call_decorator_creates_span_and_records_error(_tracer):
    from app.services.tracing.spans import traced_llm_call

    @traced_llm_call()
    async def boom():
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        import asyncio
        asyncio.run(boom())

    assert len(_tracer.spans) == 1
    assert "error" in _tracer.spans[0].attributes


def test_tool_exec_span_records_success_and_error():
    from app.services.tracing.spans import tool_exec_span

    with tool_exec_span("execute_query", {"sql": "SELECT 1"}) as span:
        span.set_attribute("gen_ai.tool.success", True)

    assert span.attributes["gen_ai.tool.name"] == "execute_query"
    assert span.attributes["gen_ai.tool.success"] is True
    assert span.attributes["gen_ai.tool.args_hash"]

    with tool_exec_span("execute_query", {}) as span2:
        span2.set_attribute("gen_ai.tool.success", False)
        span2.set_attribute("error", "query failed")

    assert span2.attributes["error"] == "query failed"


def test_tool_args_preview_honors_include_flag(monkeypatch):
    from app.services.tracing.spans import tool_call_span

    monkeypatch.setattr(_settings(), "TRACE_INCLUDE_TOOL_INPUT", False, raising=False)
    hidden = tool_call_span("execute_query", {"password": "secret"})
    assert "gen_ai.tool.args_preview" not in hidden.attributes

    monkeypatch.setattr(_settings(), "TRACE_INCLUDE_TOOL_INPUT", True, raising=False)
    shown = tool_call_span("execute_query", {"sql": "SELECT 1"})
    assert "gen_ai.tool.args_preview" in shown.attributes
    assert "SELECT 1" in shown.attributes["gen_ai.tool.args_preview"]


def test_sampling_rate_zero_skips_recorded_spans(monkeypatch, _tracer):
    monkeypatch.setattr(_settings(), "TRACE_SPAN_SAMPLING_RATE", 0.0, raising=False)

    from app.services.tracing.spans import llm_call_span

    s = llm_call_span("deepseek-v4")
    # Sampled out → plain base Span, never handed to the tracer.
    assert not isinstance(s, RecordingSpan)
    assert _tracer.spans == []


def test_fsm_and_workflow_spans_feed_metrics(monkeypatch):
    monkeypatch.setattr(_settings(), "TRACE_METRICS_ENABLED", True, raising=False)

    from app.services.tracing import metrics
    from app.services.tracing.spans import (
        fsm_run_span,
        fsm_state_span,
        workflow_node_span,
    )

    with fsm_run_span("exec-1", "general_assistant", "chat"):
        with fsm_state_span("PLAN", "exec-1"):
            with workflow_node_span("n_1", "llm") as wspan:
                wspan.set_attribute("success", True)

    snap = metrics.get_metrics().snapshot()
    assert snap["fsm_runs"]["total"] == 1
    assert snap["fsm_runs"]["errors"] == 0
    assert snap["fsm_states"]["PLAN"] == 1
    assert snap["workflow"]["llm"]["calls"] == 1
    assert snap["workflow"]["llm"]["errors"] == 0


def test_metrics_snapshot_aggregates_llm_and_tools(monkeypatch):
    monkeypatch.setattr(_settings(), "TRACE_METRICS_ENABLED", True, raising=False)

    from app.services.tracing import metrics

    m = metrics.get_metrics()
    m.record_llm(
        model="deepseek-v4", prompt_tokens=1000, completion_tokens=500,
        latency_ms=1200,
    )
    m.record_llm(
        model="deepseek-v4", prompt_tokens=1000, completion_tokens=500,
        latency_ms=800, error="boom",
    )
    m.record_tool(tool_name="execute_query", duration_ms=200, success=True)
    m.record_tool(tool_name="execute_query", duration_ms=100, success=False)

    snap = m.snapshot()
    llm = snap["llm"]["deepseek-v4"]
    assert llm["calls"] == 2
    assert llm["errors"] == 1
    assert llm["cost_usd"] > 0
    assert snap["tools"]["execute_query"]["calls"] == 2
    assert snap["tools"]["execute_query"]["errors"] == 1
    assert snap["tools"]["execute_query"]["total_latency_ms"] == 300
