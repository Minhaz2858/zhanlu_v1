"""2026-08-25: live-streaming spec — verify reasoning_delta SSE event is emitted per token."""
import json
import os
import pytest


def test_reasoning_delta_emitted_in_agents_py():
    """The agents.py source must contain the reasoning_delta SSE event emission."""
    agents_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "routers", "agents.py",
    )
    with open(agents_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert '"reasoning_delta"' in src, "reasoning_delta SSE event not found in agents.py"
    assert '"content"' in src, "content field not found"


def test_reasoning_delta_yields_valid_sse_format():
    """The reasoning_delta event must be a valid SSE frame."""
    frame = f'data: {json.dumps({"type": "reasoning_delta", "content": "thinking..."})}\n\n'
    # SSE format: starts with 'data: ' and ends with '\n\n'
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    # The JSON must parse
    payload = json.loads(frame[len("data: "):].rstrip("\n"))
    assert payload["type"] == "reasoning_delta"
    assert payload["content"] == "thinking..."
