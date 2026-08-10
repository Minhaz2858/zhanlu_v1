"""Unit tests for CheckpointSink."""

from app.services.harness.checkpoint_sink import CheckpointSink


class TestCheckpointSink:

    def test_disabled_sink_is_noop(self):
        sink = CheckpointSink(enabled=False)
        sink({"type": "llm_call", "run_id": "r1", "iteration": 0})

    def test_llm_call_event_increments_index(self):
        sink = CheckpointSink(enabled=True)
        try:
            sink({"type": "llm_call", "run_id": "r1", "iteration": 1})
        except Exception:
            pass
        assert sink._step_index == 1

    def test_step_index_increments_to_three(self):
        sink = CheckpointSink(enabled=True)
        for ev in [
            {"type": "llm_call", "run_id": "r1", "iteration": 0},
            {"type": "tool_call", "run_id": "r1", "tool_name": "x"},
            {"type": "llm_call", "run_id": "r1", "iteration": 1},
        ]:
            try:
                sink(ev)
            except Exception:
                pass
        assert sink._step_index == 3

    def test_truncation(self):
        assert len(CheckpointSink._truncate("x" * 8000, 4000)) == 4000
        assert CheckpointSink._truncate("short", 4000) == "short"

    def test_safe_json(self):
        assert CheckpointSink._safe_json(None) is None
        assert '"key"' in CheckpointSink._safe_json({"key": "value"})
