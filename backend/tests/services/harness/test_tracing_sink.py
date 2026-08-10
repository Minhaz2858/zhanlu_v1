"""Unit tests for TracingSink — all use noop tracer by default."""

from app.services.harness.tracing_sink import TracingSink


class TestTracingSink:

    def test_disabled_sink_is_noop(self):
        sink = TracingSink(enabled=False)
        sink({"type": "run_start", "agent_name": "test"})

    def test_run_lifecycle(self):
        sink = TracingSink(enabled=True)
        sink({"type": "run_start", "agent_name": "test", "run_id": "r1"})
        sink({"type": "run_finish", "success": True, "iterations": 3})

    def test_llm_call_records(self):
        sink = TracingSink(enabled=True)
        sink({"type": "llm_call", "iteration": 0, "model": "default",
              "prompt_tokens": 100, "completion_tokens": 50, "duration_ms": 500})

    def test_tool_call_records(self):
        sink = TracingSink(enabled=True)
        sink({"type": "tool_call", "tool_name": "search", "duration_ms": 200})

    def test_double_run_finish_is_safe(self):
        sink = TracingSink(enabled=True)
        sink({"type": "run_start", "agent_name": "x"})
        sink({"type": "run_finish", "success": True})
        sink({"type": "run_finish", "success": True})

    def test_finish_without_start_is_safe(self):
        sink = TracingSink(enabled=True)
        sink({"type": "run_finish", "success": True})
