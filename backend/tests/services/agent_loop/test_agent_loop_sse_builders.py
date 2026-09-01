"""Unit tests for the extracted SSE event builders (P2-12).

Covers ``app.services.agent_loop.sse_builders`` — the SSE wire format must
stay byte-compatible with the frontend ``Chat.jsx`` consumer:
- ``_emit_activity_step`` / ``_emit_phase``: frame shape + JSON payload.
- live-event taxonomy: ``_build_live_event`` cap, ``_sse_live_event``,
  ``_emit_live_event``.
- content invariant: ``_sanitize_live_event_params`` + ``_sample_rows_from_payload``.
"""
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.agent_loop.sse_builders import (
    LIVE_EVENT_TEMPLATES,
    PHASE_HEADLINES,
    _LIVE_EVENT_CAP,
    _build_live_event,
    _emit_activity_step,
    _emit_live_event,
    _emit_phase,
    _sample_rows_from_payload,
    _sanitize_live_event_params,
    _sse_live_event,
)


def _parse(frame: str) -> dict:
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    return json.loads(frame[len("data: "):].strip())


# ---------------------------------------------------------------------------
# _emit_activity_step
# ---------------------------------------------------------------------------

def test_activity_step_basic_shape():
    frame = _emit_activity_step(1, "Running query", "running")
    payload = _parse(frame)
    assert payload["type"] == "activity_step"
    step = payload["step"]
    assert step["number"] == 1
    assert step["description"] == "Running query"
    assert step["status"] == "running"


def test_activity_step_optional_fields():
    frame = _emit_activity_step(
        2, "Executing", "success", tool_name="execute_query",
        command="SELECT 1", output_preview="1 row", artifact_id="a1", duration_ms=1234,
    )
    step = _parse(frame)["step"]
    assert step["tool_name"] == "execute_query"
    assert step["command"] == "SELECT 1"
    assert step["output_preview"] == "1 row"
    assert step["artifact_id"] == "a1"
    assert step["duration_ms"] == 1234


# ---------------------------------------------------------------------------
# _emit_phase
# ---------------------------------------------------------------------------

def test_phase_uses_headline_table():
    frame = _emit_phase("goal")
    payload = _parse(frame)
    assert payload["type"] == "phase"
    assert payload["state"] == "goal"
    assert payload["verb"] == PHASE_HEADLINES["goal"][0]
    assert payload["title"] == PHASE_HEADLINES["goal"][1]


def test_phase_detail_override_and_unknown_state():
    frame = _emit_phase("unknown_state", detail="Custom")
    payload = _parse(frame)
    assert payload["title"] == "Custom"
    frame2 = _emit_phase("unknown_state")
    assert _parse(frame2)["title"] == "Unknown State"


# ---------------------------------------------------------------------------
# live events
# ---------------------------------------------------------------------------

def test_sse_live_event_wraps_container():
    event = {"type": "tool_call_started", "label_key": "tool_call_started", "params": {}}
    payload = _parse(_sse_live_event(event))
    assert payload["type"] == "live_event"
    assert payload["event"]["label_key"] == "tool_call_started"


def test_build_live_event_caps_at_limit():
    count = [0]
    events = []
    for _ in range(_LIVE_EVENT_CAP + 5):
        ev = _build_live_event("x", "y", params={"i": 1}, count=count)
        if ev is not None:
            events.append(ev)
    assert len(events) == _LIVE_EVENT_CAP
    assert count[0] == _LIVE_EVENT_CAP


def test_emit_live_event_returns_none_when_capped():
    count = [_LIVE_EVENT_CAP]
    assert _emit_live_event("x", "y", count=count) is None


def test_live_event_has_utc_timestamp():
    ev = _build_live_event("phase_enter.act", "phase_enter.act", params={})
    assert ev["ts"].endswith("+00:00") or "+00:00" in ev["ts"]


def test_finalize_done_is_accepted_label_key():
    """finalize_done (bug-fix event) must be a valid wire type + template."""
    assert "finalize_done" in LIVE_EVENT_TEMPLATES
    ev = _build_live_event("finalize_done", "finalize_done", params={})
    assert ev is not None
    assert ev["type"] == "finalize_done"
    assert ev["label_key"] == "finalize_done"
    assert "+00:00" in ev["ts"]


# ---------------------------------------------------------------------------
# content invariant
# ---------------------------------------------------------------------------

def test_sanitize_blocks_sql_and_erp_patterns():
    out = _sanitize_live_event_params({"sql": "select * from erp_t_sal_outstock", "ok": "hello"})
    assert out["sql"] == "[data]"
    assert out["ok"] == "hello"


def test_sanitize_recurses_into_nested_structures():
    out = _sanitize_live_event_params(
        {"nested": {"rows": [{"sql": "select * from x", "name": "ok"}]}}
    )
    assert out["nested"]["rows"][0]["sql"] == "[data]"
    assert out["nested"]["rows"][0]["name"] == "ok"


def test_sample_rows_from_payload_caps_and_sanitizes():
    payload = {
        "rows": [
            {"FNAME": "a", "qty": 1, "col3": 3, "col4": 4, "col5": 5, "col6": 6},
            {"FNAME": "b", "qty": 2},
        ]
    }
    sample = _sample_rows_from_payload(payload)
    assert sample["columns"] == ["FNAME", "qty", "col3", "col4", "col5"]
    assert len(sample["sample_rows"]) == 2
    assert sample["sample_rows"][0]["FNAME"] == "a"
    assert sample["sample_rows"][0]["qty"] == 1


def test_sample_rows_none_for_non_row_payloads():
    assert _sample_rows_from_payload(None) is None
    assert _sample_rows_from_payload({"text": "hello"}) is None
    assert _sample_rows_from_payload([1, 2, 3]) is None
