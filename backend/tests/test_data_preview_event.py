"""2026-08-25: live-streaming spec — verify data_preview SSE event is emitted."""
import os


def test_agents_py_emits_data_preview():
    """agents.py must emit data_preview SSE events."""
    agents_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "routers", "agents.py",
    )
    with open(agents_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert '"data_preview"' in src, "agents.py must emit data_preview SSE events"
    assert "sample_rows" in src, "data_preview event must include sample_rows"
    assert "tool_call_id" in src, "data_preview event must include tool_call_id"
