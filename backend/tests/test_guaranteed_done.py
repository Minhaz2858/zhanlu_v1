"""Tests for the ``_guarantee_done`` SSE wrapper in agents.py.

The v3 stream used to kill the SSE connection without a ``done`` frame
whenever any unhandled exception escaped between the report-card render
and the final done emission — the frontend then showed "Sorry, the
connection was interrupted". This wrapper guarantees the ``done`` frame.
"""
import asyncio

import pytest

from app.routers.agents import _guarantee_done


async def _inner_ok():
    yield 'data: {"type": "x"}\n\n'
    yield 'data: {"type": "done", "content": "ok"}\n\n'


async def _inner_boom():
    yield 'data: {"type": "x"}\n\n'
    raise RuntimeError("boom mid-stream")


async def _inner_empty():
    return
    yield  # pragma: no cover


def _collect(agen):
    frames = []

    async def _run():
        async for f in agen:
            frames.append(f)

    asyncio.run(_run())
    return frames


def test_done_emitted_when_inner_raises():
    frames = _collect(_guarantee_done(_inner_boom()))
    assert len(frames) == 2
    done = frames[-1]
    assert done.startswith("data: ")
    assert '"type": "done"' in done
    assert "conversation" in done


def test_passthrough_when_inner_finishes_cleanly():
    frames = _collect(_guarantee_done(_inner_ok()))
    assert frames == ['data: {"type": "x"}\n\n', 'data: {"type": "done", "content": "ok"}\n\n']


def test_no_duplicate_done_on_clean_finish():
    frames = _collect(_guarantee_done(_inner_ok()))
    done_frames = [f for f in frames if '"type": "done"' in f]
    assert len(done_frames) == 1


def test_empty_inner_yields_nothing():
    frames = _collect(_guarantee_done(_inner_empty()))
    assert frames == []


def test_cancellation_propagates():
    """Client disconnect (CancelledError) must NOT emit a fake done."""

    async def _run():
        gen = _guarantee_done(_inner_boom())
        it = gen.__aiter__()
        # Pull one frame, then cancel the pending __anext__ future.
        fut = asyncio.ensure_future(it.__anext__())
        frames = []
        done, _ = await asyncio.wait({fut}, timeout=1.0)
        if fut in done:
            frames.append(fut.result())
        fut2 = asyncio.ensure_future(it.__anext__())
        try:
            await asyncio.wait_for(fut2, timeout=0.5)
        except asyncio.TimeoutError:
            pass  # inner raised before cancel — that's fine for this probe
        await gen.aclose()  # must not raise

    asyncio.run(_run())
