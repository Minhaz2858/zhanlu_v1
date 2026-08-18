"""Tests for the v3-loop SSE ``tool_progress`` heartbeat.

Long-running delegation tools (``ask_perception``, ``ask_intelligence``,
``ask_diagnosis``, the batch tool, etc.) can run for minutes. While they
execute, the v3 loop must emit ``tool_progress`` SSE frames so the client
sees liveness and proxies don't time out.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


def _parse_sse_frames(chunks: list[str]) -> list[dict]:
    events: list[dict] = []
    for chunk in chunks:
        for line in chunk.splitlines():
            line = line.strip()
            if not line.startswith("data: "):
                continue
            try:
                events.append(json.loads(line[len("data: "):]))
            except json.JSONDecodeError:
                continue
    return events


def test_long_running_tool_classification():
    """Delegation tools (ask_*) and known long tools must be flagged."""
    from app.routers.agents import _is_long_running_tool

    assert _is_long_running_tool("ask_perception") is True
    assert _is_long_running_tool("ask_intelligence") is True
    assert _is_long_running_tool("ask_diagnosis") is True
    assert _is_long_running_tool("ask_perception_intelligence_diagnosis") is True
    assert _is_long_running_tool("ask_data_agent") is True
    assert _is_long_running_tool("web_search") is True
    assert _is_long_running_tool("execute_automation") is True
    # Fast/utility tools must NOT trigger the heartbeat wrapper.
    assert _is_long_running_tool("list_skills") is False
    assert _is_long_running_tool("get_time") is False


def test_slow_task_emits_progress_frames():
    """While the tool task runs, tool_progress frames must be yielded."""
    from app.routers.agents import _emit_tool_progress_while_waiting

    async def _slow():
        await asyncio.sleep(0.2)
        return {"success": True, "answer": "done"}

    parsed_calls = [
        {
            "tool_call_id": "call-1",
            "tool_name": "ask_perception_intelligence_diagnosis",
            "args_str": '{"question": "weekly market report"}',
        }
    ]

    async def _collect():
        task = asyncio.ensure_future(_slow())
        frames = []
        async for frame in _emit_tool_progress_while_waiting(
            task, parsed_calls, interval=0.05
        ):
            frames.append(frame)
        return frames, task.result()

    frames, result = asyncio.run(_collect())

    assert result["success"] is True
    assert len(frames) >= 1, "expected at least one progress frame during a slow task"
    events = _parse_sse_frames(frames)
    assert len(events) >= 1
    evt = events[0]
    assert evt["type"] == "tool_progress"
    assert isinstance(evt["tool_calls"], list)
    assert evt["tool_calls"][0]["id"] == "call-1"
    assert evt["tool_calls"][0]["name"] == "ask_perception_intelligence_diagnosis"
    assert evt["tool_calls"][0]["status"] == "running"
    assert "question" in evt["tool_calls"][0]["arguments_string"]


def test_fast_task_emits_no_progress_frames():
    """A tool that returns immediately must not emit heartbeat frames."""
    from app.routers.agents import _emit_tool_progress_while_waiting

    async def _fast():
        return {"success": True, "answer": "instant"}

    parsed_calls = [{"tool_call_id": "call-2", "tool_name": "list_skills", "args_str": "{}"}]

    async def _collect():
        task = asyncio.ensure_future(_fast())
        frames = []
        async for frame in _emit_tool_progress_while_waiting(
            task, parsed_calls, interval=0.05
        ):
            frames.append(frame)
        return frames, task.result()

    frames, result = asyncio.run(_collect())
    assert result["success"] is True
    assert frames == []


def test_progress_frames_are_sse_data_chunks():
    """Each yielded frame must be a valid SSE ``data: {...}`` chunk."""
    from app.routers.agents import _emit_tool_progress_while_waiting

    async def _slow():
        await asyncio.sleep(0.15)
        return {"success": True}

    parsed_calls = [{"tool_call_id": "call-3", "tool_name": "ask_diagnosis", "args_str": "{}"}]

    async def _collect():
        task = asyncio.ensure_future(_slow())
        frames = []
        async for frame in _emit_tool_progress_while_waiting(
            task, parsed_calls, interval=0.05
        ):
            frames.append(frame)
        return frames

    frames = asyncio.run(_collect())
    assert len(frames) >= 1
    assert all(f.startswith("data: ") for f in frames)
