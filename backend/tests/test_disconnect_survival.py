"""Regression test: v3 SSE stream must survive client disconnect.

The agent loop used to run INSIDE the StreamingResponse generator. A
client disconnect (browser tab closed, remote-browser session recycled)
cancelled the loop mid-turn, so the final assistant message was never
persisted — dashboard builds silently died. ``_disconnect_safe_stream``
detaches the loop into a background task; the wrapper yields from a
queue and on disconnect does NOT cancel the pump, so the loop finishes
and persists its result.
"""

import asyncio

import pytest

from app.routers.agents import _disconnect_safe_stream


def _make_fake_loop(frames, done_log):
    """Factory for a fake agent-loop generator that logs completion."""
    async def _gen():
        try:
            for f in frames:
                yield f
            done_log.append("loop_completed")
        finally:
            done_log.append("loop_closed")
    return _gen


@pytest.mark.asyncio
async def test_stream_survives_client_disconnect():
    done_log: list[str] = []
    factory = _make_fake_loop([f"data: frame{i}\n\n" for i in range(5)], done_log)

    gen = _disconnect_safe_stream(factory)
    it = gen.__aiter__()

    # Client receives the first frames...
    first = await it.__anext__()
    assert first == "data: frame0\n\n"
    second = await it.__anext__()
    assert second == "data: frame1\n\n"

    # ...then disconnects: Starlette calls aclose() on the generator.
    await gen.aclose()

    # Give the detached pump task a chance to finish the loop.
    await asyncio.sleep(0.2)

    # The loop must have COMPLETED (not cancelled) — final frames produced,
    # generator closed, sentinel consumed.
    assert "loop_completed" in done_log
    assert "loop_closed" in done_log
    assert done_log.index("loop_completed") < done_log.index("loop_closed")


@pytest.mark.asyncio
async def test_stream_normal_completion_forwards_all_frames():
    done_log: list[str] = []
    frames = [f"data: f{i}\n\n" for i in range(3)]
    gen = _disconnect_safe_stream(_make_fake_loop(frames, done_log))

    received = [f async for f in gen]

    assert received == frames
    assert "loop_completed" in done_log
    assert "loop_closed" in done_log


@pytest.mark.asyncio
async def test_stream_surfaces_loop_exception_as_error_frame():
    async def _boom():
        yield "data: before\n\n"
        raise RuntimeError("provider hiccup")

    gen = _disconnect_safe_stream(lambda: _boom())
    received = [f async for f in gen]

    assert received[0] == "data: before\n\n"
    assert len(received) == 2
    assert '"type": "error"' in received[1]
    assert "provider hiccup" in received[1]
