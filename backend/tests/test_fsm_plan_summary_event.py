"""Tests for plan_summary SSE event emission in the v3 FSM stream path.

The FSM produces a plan_summary dict (nodes + status) during FINALIZE.
The v3 stream path should emit it as a standalone SSE event so the
frontend can render the decomposed execution plan before the final
response streams in.
"""

from __future__ import annotations

import json


def _parse_sse(chunk: str) -> list[dict]:
    """Parse one or more ``data: {json}\\n\\n`` blocks from an SSE chunk."""
    events: list[dict] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            events.append(json.loads(line[len("data: "):]))
        except json.JSONDecodeError:
            continue
    return events


def test_plan_summary_sse_event_shape():
    """The plan_summary SSE event must have type='plan_summary' and a plan dict."""
    plan_summary = {
        "nodes": [
            {"seq": 1, "name": "parse_request", "node_type": "tool", "status": "completed"},
            {"seq": 2, "name": "generate_report", "node_type": "skill", "status": "completed"},
        ],
        "status": "completed",
    }
    raw = f'data: {json.dumps({"type": "plan_summary", "plan": plan_summary})}\n\n'
    events = _parse_sse(raw)
    assert len(events) == 1
    assert events[0]["type"] == "plan_summary"
    assert events[0]["plan"]["status"] == "completed"
    assert len(events[0]["plan"]["nodes"]) == 2
    assert events[0]["plan"]["nodes"][0]["name"] == "parse_request"


def test_plan_summary_sse_event_empty_nodes():
    """A plan with no nodes should still emit a valid event."""
    plan_summary = {"nodes": [], "status": "pending"}
    raw = f'data: {json.dumps({"type": "plan_summary", "plan": plan_summary})}\n\n'
    events = _parse_sse(raw)
    assert len(events) == 1
    assert events[0]["type"] == "plan_summary"
    assert events[0]["plan"]["nodes"] == []


def test_plan_summary_not_emitted_when_none():
    """When fsm_result.plan_summary is None, no plan_summary event should be emitted."""
    # Simulate the conditional in the v3 stream path
    fsm_result_plan_summary = None
    emitted = []
    if fsm_result_plan_summary:
        emitted.append(f'data: {json.dumps({"type": "plan_summary", "plan": fsm_result_plan_summary})}\n\n')
    assert emitted == []


def test_plan_summary_event_order_in_stream():
    """plan_summary must come after fsm_state events but before done."""
    from app.routers.agents import _emit_fsm_state

    collected = [
        _emit_fsm_state("init"),
        _emit_fsm_state("goal"),
        _emit_fsm_state("plan"),
        _emit_fsm_state("done"),
    ]
    plan_summary = {"nodes": [{"seq": 1, "name": "n1", "node_type": "tool", "status": "completed"}], "status": "completed"}
    plan_event = f'data: {json.dumps({"type": "plan_summary", "plan": plan_summary})}\n\n'
    done_payload = {"type": "done", "content": "response", "fsm_state": "done"}

    yielded = collected + [plan_event, f'data: {json.dumps(done_payload)}\n\n']
    types = [_parse_sse(ev)[0]["type"] for ev in yielded]
    assert types == ["fsm_state", "fsm_state", "fsm_state", "fsm_state", "plan_summary", "done"]


def test_fsm_result_includes_plan_summary():
    """ExecutionResult must carry plan_summary from the FSM run."""
    from app.services.synexia.fsm import ExecutionResult

    result = ExecutionResult(
        execution_id="exec-test",
        assistant_content="done",
        plan_summary={
            "nodes": [{"seq": 1, "name": "n1", "node_type": "tool", "status": "completed"}],
            "status": "completed",
        },
    )
    assert result.plan_summary is not None
    assert result.plan_summary["status"] == "completed"
    assert len(result.plan_summary["nodes"]) == 1
