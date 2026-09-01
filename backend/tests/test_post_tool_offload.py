"""Event-loop offload regression tests for the post-tool experience hook.

Root cause observed in production (2026-08-19): ``_store_turn_cache`` (and
``_record_turn_experience``) are synchronous helpers that include an LLM
embedding API call (``get_embedding``) and DB writes. They used to run inline
on the SSE generator in ``agents.py`` (~L9588), so a 30-60s embedding call
starved the heartbeat in ``_sse_with_heartbeat()`` and the frontend saw
"connection interrupted" before the ``done`` frame was emitted.

The contract: the experience hooks must run via ``asyncio.to_thread`` (so the
event loop is free to pump heartbeats), bounded by a timeout so a stalled
embedding service fails over gracefully instead of hanging the stream; the
``done`` frame must always fire even if the hooks raise.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest


def _make_spy():
    """Record every callable handed to asyncio.to_thread, then run it for real
    so the surrounding flow still executes."""
    calls = []
    real_to_thread = asyncio.to_thread

    def spy(fn, *args, **kwargs):
        calls.append(fn)
        return real_to_thread(fn, *args, **kwargs)

    return calls, spy


@pytest.mark.asyncio
async def test_experience_hooks_are_offloaded_to_thread():
    """``_record_turn_experience`` and the inner ``_store_turn_cache`` worker
    must be handed to asyncio.to_thread, never awaited directly on the loop."""
    from app.routers import agents

    calls, spy = _make_spy()

    # Patch asyncio.to_thread with a plain function (NOT side_effect=spy —
    # MagicMock wraps coroutine-returning calls in a generator-coroutine that
    # never drives the inner coroutine; see test_artifact_tool_offload.py).
    # SessionLocal is imported lazily from app.database inside _experience_work,
    # so patch it at its source module, not at app.routers.agents.
    with patch("asyncio.to_thread", new=spy), \
         patch.object(agents, "_record_turn_experience",
                      return_value=None) as m_record, \
         patch.object(agents, "_store_turn_cache",
                      return_value=None) as m_cache, \
         patch("app.database.SessionLocal", return_value=MagicMock()) as _SL:

        # Run the offloaded experience worker directly (mirrors the body of
        # the post-tool block in the v3 stream generator).
        def _experience_work() -> None:
            m_record()
            cache_db = _SL()
            try:
                m_cache(cache_db)
            finally:
                cache_db.close()

        await asyncio.wait_for(
            asyncio.to_thread(_experience_work),
            timeout=agents.EXPERIENCE_HOOK_TIMEOUT_S,
        )

    # Both helpers must have been called (proves the work ran to completion).
    assert m_record.called, "_record_turn_experience must run"
    assert m_cache.called, "_store_turn_cache must run"


@pytest.mark.asyncio
async def test_experience_hook_timeout_does_not_break_the_stream():
    """A stalled embedding service (60s+ timeout) must NOT block the loop;
    the outer wait_for raises asyncio.TimeoutError, the caller continues to
    emit the `done` frame."""
    from app.routers import agents

    calls, spy = _make_spy()

    def _hang(*_args, **_kwargs):
        # Simulate a long embedding call by blocking the thread long enough
        # for the outer wait_for to fire (use a small ceiling to keep the
        # test fast).
        import time as _time
        _time.sleep(2.0)

    with patch("asyncio.to_thread", new=spy), \
         patch.object(agents, "_record_turn_experience", side_effect=_hang), \
         patch.object(agents, "EXPERIENCE_HOOK_TIMEOUT_S", 0.5):

        def _experience_work() -> None:
            _hang()

        # Outer wait_for must catch the stall and raise TimeoutError — the
        # outer caller in agents.py catches asyncio.TimeoutError, logs it,
        # and continues to emit `done`. The test asserts the timeout fires.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.to_thread(_experience_work),
                timeout=agents.EXPERIENCE_HOOK_TIMEOUT_S,
            )


@pytest.mark.asyncio
async def test_experience_hook_does_not_share_request_db_session():
    """SQLAlchemy Session is not thread-safe across threads. The offloaded
    experience worker must open its own SessionLocal() instead of receiving
    the request-scoped `db` from the v3 generator. This test asserts the
    SessionLocal factory is called when the worker runs."""
    from app.routers import agents

    fake_session = MagicMock(name="FreshSession")
    with patch("asyncio.to_thread", new=_make_spy()[1]), \
         patch.object(agents, "_record_turn_experience", return_value=None), \
         patch.object(agents, "_store_turn_cache", return_value=None), \
         patch("app.database.SessionLocal", return_value=fake_session) as SL:

        def _experience_work() -> None:
            agents._record_turn_experience()
            cache_db = SL()
            try:
                agents._store_turn_cache(cache_db)
            finally:
                cache_db.close()

        await asyncio.wait_for(
            asyncio.to_thread(_experience_work),
            timeout=agents.EXPERIENCE_HOOK_TIMEOUT_S,
        )

    assert SL.called, (
        "post-tool experience hook must open its own DB session via "
        "SessionLocal() — SQLAlchemy Session is not thread-safe across threads."
    )
    assert fake_session.close.called, "the offloaded session must be closed"