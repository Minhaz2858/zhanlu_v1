"""Tests for the mid-turn steer endpoint + v3 drain (P2 Task 2)."""

from __future__ import annotations

import json

from pathlib import Path


def test_emit_steer_payload_shape():
    from app.routers.agents import _emit_steer
    raw = _emit_steer(["hello", "world"])
    assert raw.startswith("data: ")
    payload = json.loads(raw[len("data: "):].strip())
    assert payload["type"] == "steer"
    assert payload["messages"] == ["hello", "world"]


def test_emit_steer_with_empty_list():
    from app.routers.agents import _emit_steer
    raw = _emit_steer([])
    payload = json.loads(raw[len("data: "):].strip())
    assert payload == {"type": "steer", "messages": []}


def test_discard_steer_swallows_exceptions(monkeypatch):
    from app.routers import agents as _a
    def _boom(_cid):
        raise RuntimeError("discard boom")
    monkeypatch.setattr(_a.steer_bus, "discard", _boom)
    _a._discard_steer("conv-anything")


def test_discard_steer_calls_steer_bus_discard(monkeypatch):
    from app.routers import agents as _a
    called = []
    def _fake(cid):
        called.append(cid)
    monkeypatch.setattr(_a.steer_bus, "discard", _fake)
    _a._discard_steer("conv-X")
    assert called == ["conv-X"]


def test_steer_endpoint_route_exists():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    assert "/steer" in src
    assert '"/apps/{app_id}/agents/conversations/{conversation_id}/steer"' in src


def test_steer_endpoint_validates_empty_message():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    assert "message is required" in src
    assert "HTTPException(status_code=400" in src


def test_steer_endpoint_caps_message_length():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    assert "8 * 1024" in src or "8192" in src


def test_steer_endpoint_returns_429_on_full_queue():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    assert "status_code=429" in src
    assert "steer queue full" in src


def test_steer_endpoint_returns_ok_on_success():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    assert "ok" in src and "queued" in src


def test_v3_event_stream_drains_at_iteration_top():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    assert "for iteration in range(MAX_TOOL_ITERATIONS):" in src
    assert "steer_bus.drain(conversation_id)" in src
    drain_idx = src.find("steer_bus.drain(conversation_id)")
    llm_idx = src.find("_call_llm_with_tools", drain_idx)
    assert drain_idx != -1 and llm_idx != -1 and drain_idx < llm_idx


def test_v3_event_stream_injects_steer_into_llm_messages():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    assert 'llm_messages.append({"role": "user", "content": _sm})' in src


def test_v3_event_stream_emits_steer_sse_event():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    assert "yield _emit_steer(_steer_msgs)" in src


def test_discard_steer_called_in_done_path():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    # The v3 stream emits its `done` frame inside a try/finally whose finally
    # block cleans up the steer bus — assert the cleanup sits right after the
    # done-emission region (anchored on the cleanup comment).
    done_idx = src.find('"type": "done", "content": accumulated_content or assistant_content')
    assert done_idx != -1
    cleanup_idx = src.find("# Clean up the steer bus for this conversation", done_idx)
    assert cleanup_idx != -1
    window = src[cleanup_idx:cleanup_idx + 300]
    assert "_discard_steer(conversation_id)" in window


def test_discard_steer_called_in_paused_paths():
    """Each paused yield (decision_summary / approval) must call discard.

    The module docstring also mentions "type": "paused" once, so we count
    only the actual yields (those followed by the same `return` shape).
    """
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    # Count actual yields (not docstring mentions).
    paused_yield_count = src.count(
        'yield f\'data: {json.dumps({"type": "paused", "reason": "awaiting_decision_summary"'
    ) + src.count(
        'yield f\'data: {json.dumps({"type": "paused", "conversation": conv.to_dict()})}\\n\\n\''
    )
    assert paused_yield_count >= 2
    # Each paused yield must be followed by a discard call.
    assert src.count("_discard_steer(conversation_id)") >= paused_yield_count


def test_discard_steer_called_in_error_path():
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py").read_text()
    # v3 stream error handling: _persist_stream_error yields the error event,
    # then the call site must discard the steer bus for the conversation.
    err_idx = src.find("for _err_event in _persist_stream_error(")
    assert err_idx != -1, "Error emit loop not found"
    window = src[err_idx:err_idx + 400]
    assert "_discard_steer(conversation_id)" in window


def test_full_steer_flow_in_memory():
    from app.services import steer_bus
    steer_bus._QUEUES.clear()
    cid = "conv-integration"
    msg = "stop and switch to English"
    assert steer_bus.enqueue(cid, msg) is True
    drained = steer_bus.drain(cid)
    assert drained == [msg]
    assert steer_bus.drain(cid) == []
    steer_bus.discard(cid)
    assert cid not in steer_bus._QUEUES
    steer_bus._QUEUES.clear()
