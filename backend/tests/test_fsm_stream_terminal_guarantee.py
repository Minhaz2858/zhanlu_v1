"""R1 regression tests — terminal-frame guarantee on the FSM SSE route.

Defect (2026-10-27): the FSM route's StreamingResponse omitted
``_guarantee_done``, which the ReAct route has. When ``_fsm_event_stream``
terminated before yielding its ``done`` frame, the client received a clean
response with NO terminal event and the optimistically-created empty assistant
bubble was never filled (live: conv 1414bb16 turn 1, ``content_len = 0``;
``agent_invocations`` had no row for it, proving the generator never reached
the code after ``yield done_payload``).

Scope of R1: PROTOCOL COMPLETION ONLY. It guarantees the client always
receives a terminal event. It does NOT supply answer content (R3 handles the
grounded fallback).

Tests required by the review:
  1. Normal FSM completion emits exactly one terminal ``done``.
  2. An exception before ``done`` still unblocks the client with a terminal event.
  3. Existing ``done`` events are not duplicated.
  4. ReAct behavior remains unchanged.

Deterministic; no network, no DB.
"""

from __future__ import annotations

import inspect
import json
import re

import pytest

from app.routers.agents import (
    _disconnect_safe_stream,
    _guarantee_done,
    _sse_with_heartbeat,
)

_TERMINAL = ("done", "error", "paused", "interrupted", "flow_saved")


def _frame(payload: dict) -> str:
    return "data: " + json.dumps(payload) + "\n\n"


def _types(frames) -> list[str]:
    """Extract SSE 'type' values from collected frames."""
    out = []
    for f in frames:
        if not isinstance(f, str) or '"type"' not in f:
            continue
        m = re.search(r'"type":\s*"([^"]+)"', f)
        if m:
            out.append(m.group(1))
    return out


async def _collect(agen) -> list:
    return [f async for f in agen]


async def _agen(*frames):
    for f in frames:
        yield f


# ── Test 1: normal completion → exactly one done ─────────────────────────
def test_normal_completion_emits_exactly_one_done():
    async def run():
        inner = _agen(
            _frame({"type": "delta", "content": "hello"}),
            _frame({"type": "done", "content": "hello"}),
        )
        return await _collect(_guarantee_done(inner))

    frames = _collect_async(run)
    assert _types(frames).count("done") == 1, _types(frames)
    assert _types(frames) == ["delta", "done"]


# ── Test 3: an existing done is NEVER duplicated ─────────────────────────
def test_existing_done_is_not_duplicated():
    async def run():
        inner = _agen(
            _frame({"type": "done", "content": "answer"}),
        )
        return await _collect(_guarantee_done(inner))

    frames = _collect_async(run)
    assert _types(frames) == ["done"]
    # The original payload must pass through untouched.
    assert '"content": "answer"' in frames[0]


def test_paused_and_error_count_as_terminal():
    for t in ("paused", "error", "interrupted", "flow_saved"):
        async def run(_t=t):
            inner = _agen(_frame({"type": _t, "content": ""}))
            return await _collect(_guarantee_done(inner))

        frames = _collect_async(run)
        assert _types(frames) == [t], (t, _types(frames))


# ── Test 2: exception before done still unblocks the client ──────────────
def test_exception_before_done_emits_terminal():
    async def run():
        async def boom():
            yield _frame({"type": "delta", "content": "partial"})
            raise RuntimeError("serialization crash in the post-work section")

        return await _collect(_guarantee_done(boom()))

    frames = _collect_async(run)
    types = _types(frames)
    assert "delta" in types
    # The client MUST get a terminal event.
    assert any(t in _TERMINAL for t in types), types
    assert types.count("done") == 1


def test_silent_early_return_emits_terminal():
    """The exact live shape: the generator simply returns with no done."""

    async def run():
        async def silent():
            yield _frame({"type": "delta", "content": "partial"})
            return  # no terminal frame

        return await _collect(_guarantee_done(silent()))

    frames = _collect_async(run)
    types = _types(frames)
    assert types, "expected at least one frame"
    assert any(t in _TERMINAL for t in types), f"no terminal frame: {types}"


# ── Test 4 (unit): cancellation is re-raised, not swallowed ──────────────
def test_cancellation_is_reraised():
    import asyncio

    async def run():
        async def cancelled():
            yield _frame({"type": "delta", "content": "x"})
            raise asyncio.CancelledError()

        return await _collect(_guarantee_done(cancelled()))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())


# ── Composition test: the real FSM wrapper stack ─────────────────────────
def test_full_fsm_wrapper_stack_terminates_on_silent_return():
    """_sse_with_heartbeat(_guarantee_done(_disconnect_safe_stream(gen)))
    must still deliver a terminal frame when the inner generator is silent."""

    async def run():
        async def silent_fsm():
            yield _frame({"type": "delta", "content": "answer in progress"})
            return

        stack = _sse_with_heartbeat(
            _guarantee_done(_disconnect_safe_stream(silent_fsm))
        )
        return await _collect(stack)

    frames = _collect_async(run)
    types = _types(frames)
    assert any(t in _TERMINAL for t in types), f"no terminal frame: {types}"


def test_full_stack_single_done_on_normal_completion():
    async def run():
        async def normal_fsm():
            yield _frame({"type": "plan_summary", "plan": []})
            yield _frame({"type": "delta", "content": "hi"})
            yield _frame({"type": "done", "content": "hi"})

        stack = _sse_with_heartbeat(
            _guarantee_done(_disconnect_safe_stream(normal_fsm))
        )
        return await _collect(stack)

    frames = _collect_async(run)
    assert _types(frames).count("done") == 1


# ── Test 4 (source contract): both routes carry the guarantee ────────────
def test_both_routes_carry_the_terminal_guarantee():
    """Guards against R1 regressing: FSM and ReAct stacks must both include
    _guarantee_done inside _sse_with_heartbeat."""
    import app.routers.agents as agents_mod

    src = inspect.getsource(agents_mod.add_message_stream)
    # Collapse whitespace so multi-line formatting doesn't matter.
    flat = re.sub(r"\s+", " ", src)
    wrapped = flat.count("_sse_with_heartbeat( _guarantee_done( _disconnect_safe_stream(")
    # Some call sites may format differently; count _guarantee_done overall.
    assert "_guarantee_done" in flat
    hits = len(re.findall(r"_sse_with_heartbeat\(\s*_guarantee_done\(", flat))
    assert hits >= 2, (
        f"expected the terminal guarantee on BOTH the FSM and ReAct routes; "
        f"found {hits} (wrapped={wrapped})"
    )


# ── helper: run a coroutine to completion under a fresh loop ─────────────
def _collect_async(coro_factory):
    import asyncio

    return asyncio.run(coro_factory())
