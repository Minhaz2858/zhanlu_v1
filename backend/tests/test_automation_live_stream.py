"""Tests for the automation execution live SSE stream (Phase 4, Task 2).

Tests the ``_execution_event_stream`` async generator directly with a mocked
DB session (no FastAPI app / real DB needed). The endpoint
``stream_execution_events`` is a thin StreamingResponse wrapper around it.
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
from fastapi import HTTPException

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.routers import automation_api as api


async def _collect(agen):
    """Drain an async generator into a list of yielded strings."""
    out = []
    async for y in agen:
        out.append(y)
    return out


def _sse_events(yields):
    """Parse 'data: {json}' lines from a list of yielded strings."""
    out = []
    for y in yields:
        for line in y.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[len("data:"):].strip()))
    return out


def _mock_session(first_returns):
    """Return a MagicMock db whose query().filter().first() yields the given
    sequence of execution objects (one per generator iteration)."""
    db = MagicMock()
    if isinstance(first_returns, list):
        db.query.return_value.filter.return_value.first.side_effect = first_returns
    else:
        db.query.return_value.filter.return_value.first.return_value = first_returns
    return db


def _patches(db):
    """Patch the session factory + zero the poll interval (no real sleeps)."""
    return (
        patch("app.routers.automation_api.SessionLocal", return_value=db),
        patch("app.routers.automation_api.SSE_POLL_INTERVAL", 0),
    )


async def test_completed_execution_yields_done_event():
    """A completed execution streams its activity steps then a terminal
    'done' event and stops."""
    exec_row = SimpleNamespace(
        id="exec-1", status="completed",
        activity_steps=[{"number": 1, "title": "done"}],
        current_phase=None, output_text="hello", error=None,
    )
    db = _mock_session(exec_row)
    p1, p2 = _patches(db)
    with p1, p2:
        events = _sse_events(await _collect(api._execution_event_stream("exec-1")))

    types = [e.get("type") for e in events]
    assert "activity_steps" in types
    assert "done" in types
    done = next(e for e in events if e["type"] == "done")
    assert done["content"] == "hello"
    # done must be the LAST event (stream terminates).
    assert types[-1] == "done"


async def test_failed_execution_yields_error_event():
    """A failed execution streams a terminal 'error' event with the message."""
    exec_row = SimpleNamespace(
        id="exec-2", status="failed",
        activity_steps=[], current_phase=None,
        output_text="", error="Run timed out after 600s",
    )
    db = _mock_session(exec_row)
    p1, p2 = _patches(db)
    with p1, p2:
        events = _sse_events(await _collect(api._execution_event_stream("exec-2")))

    types = [e.get("type") for e in events]
    assert types[-1] == "error"
    err = next(e for e in events if e["type"] == "error")
    assert "timed out" in err["message"].lower()


async def test_running_then_completed_emits_progress_then_done():
    """A run that is 'running' on the first poll and 'completed' on the next
    streams a phase/activity update then the done event."""
    running = SimpleNamespace(
        id="exec-3", status="running",
        activity_steps=[{"number": 1, "title": "working"}],
        current_phase="Fabricating", output_text="", error=None,
    )
    completed = SimpleNamespace(
        id="exec-3", status="completed",
        activity_steps=[{"number": 1, "title": "working"}, {"number": 2, "title": "done"}],
        current_phase="Crystallizing", output_text="final", error=None,
    )
    db = _mock_session([running, completed])
    p1, p2 = _patches(db)
    with p1, p2:
        events = _sse_events(await _collect(api._execution_event_stream("exec-3")))

    types = [e.get("type") for e in events]
    # First poll: activity_steps + phase emitted (running, no terminal).
    assert "activity_steps" in types
    assert "phase" in types
    # Then completed -> done.
    assert types[-1] == "done"
    done = next(e for e in events if e["type"] == "done")
    assert done["content"] == "final"


async def test_missing_execution_yields_error_event():
    """When the execution row disappears, the stream emits an error and stops."""
    db = _mock_session(None)  # .first() returns None
    p1, p2 = _patches(db)
    with p1, p2:
        events = _sse_events(await _collect(api._execution_event_stream("nope")))

    assert events and events[-1]["type"] == "error"


async def test_endpoint_404_for_missing_execution():
    """The endpoint's existence pre-check returns HTTP 404 for an unknown id
    (rather than opening an empty/error stream)."""
    db = _mock_session(None)
    p1, p2 = _patches(db)
    with p1, p2:
        with pytest.raises(HTTPException) as ei:
            await api.stream_execution_events("does-not-exist")
    assert ei.value.status_code == 404
