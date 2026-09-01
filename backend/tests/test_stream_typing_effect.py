"""2026-08-25: live-streaming spec — typing-effect helper for search queries."""
import json
import pytest


@pytest.mark.asyncio
async def test_stream_typing_effect_yields_chunks():
    """_stream_typing_effect must yield search_query_delta events covering the full query."""
    from app.services.agent_loop.streaming_helpers import _stream_typing_effect
    frames = []
    async for frame in _stream_typing_effect("hello world", "tc-1"):
        frames.append(frame)
    # At least 2 frames (partial at midpoint, full at end)
    assert len(frames) >= 2
    # Last frame must contain the full query
    assert "hello world" in frames[-1]
    assert "search_query_delta" in frames[-1]
    assert "tc-1" in frames[-1]


@pytest.mark.asyncio
async def test_stream_typing_effect_empty_query():
    """An empty query must yield no frames."""
    from app.services.agent_loop.streaming_helpers import _stream_typing_effect
    frames = []
    async for frame in _stream_typing_effect("", "tc-empty"):
        frames.append(frame)
    assert frames == []


@pytest.mark.asyncio
async def test_stream_typing_effect_short_query():
    """A very short query must still yield at least one frame with the full text."""
    from app.services.agent_loop.streaming_helpers import _stream_typing_effect
    frames = []
    async for frame in _stream_typing_effect("hi", "tc-short"):
        frames.append(frame)
    assert len(frames) >= 1
    # Parse the last frame
    last = frames[-1]
    assert last.startswith("data: ")
    payload = json.loads(last[len("data: "):].rstrip("\n"))
    assert payload["type"] == "search_query_delta"
    assert payload["partial"] == "hi"
    assert payload["tool_call_id"] == "tc-short"
