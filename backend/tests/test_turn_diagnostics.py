"""Stage 0 diagnostics — behavioural guarantees (2026-10-27).

These tests prove the diagnostics layer is OBSERVABILITY ONLY. They do not
assert source strings: each one exercises real behaviour and asserts
observable outcomes.

They must fail if a future change makes diagnostics alter product behaviour:

  1. diagnostics never raise, even on hostile inputs;
  2. the frame stream is forwarded byte-identically (incl. empty strings and
     unicode) and in the same order;
  3. exceptions and cancellation propagate unchanged (diagnostics never
     swallow them);
  4. the turn id is stable inside a scope, unique per scope, and restored on
     exit (no leakage between turns);
  5. the turn id propagates through ``asyncio.to_thread`` (the FSM path);
  6. counters record guard / replacement / terminal observations;
  7. logging never contains answer text (lengths only).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.services import turn_diagnostics as td


# ── 1. never raises ──────────────────────────────────────────────────────
def test_diagnostics_never_raise_on_hostile_input():
    class Bad:
        def __str__(self):
            raise RuntimeError("boom")
        __repr__ = __str__

    # Each call must return normally despite adversarial objects.
    td.note_guard(Bad(), action=Bad())
    td.note_guard("g", before_len=Bad(), after_len=Bad(), iteration=Bad())
    td.note_tool_execution(Bad(), ok=Bad(), execution_id=Bad(), duration_ms=Bad())
    td.note_stream_exit(Bad(), exception=Bad(), duration_ms=Bad())
    td.note_terminal(Bad(), content_len=Bad(), persist_status=Bad())
    td.note_iteration(Bad(), content_len=Bad())
    with td.time_finalization(Bad()):
        pass


def test_diagnostics_tolerate_bad_types_in_scope():
    with td.turn_scope(conversation_id=object(), client_turn_id=object()) as t:
        assert isinstance(t, str) and len(t) == 32


# ── 2. frame transparency ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_frames_forwarded_identically_and_in_order():
    frames_in = [
        'data: {"type": "delta", "content": "héllo "}',
        'data: {"type": "delta", "content": ""}',          # empty content frame
        ": ping\n\n",                                      # heartbeat comment
        'data: {"type": "done", "content": "日本語 ✓"}',
    ]

    async def inner():
        for f in frames_in:
            yield f

    async def wrapped():
        with td.turn_scope(conversation_id="c1"):
            async for f in inner():
                yield f

    assert [f async for f in wrapped()] == frames_in


# ── 3. exceptions / cancellation propagate ───────────────────────────────
@pytest.mark.asyncio
async def test_exceptions_propagate_unchanged():
    class Boom(ValueError):
        pass

    async def inner():
        yield "a"
        raise Boom("kaboom")

    async def wrapped():
        with td.turn_scope(conversation_id="c1"):
            async for f in inner():
                yield f

    got = []
    with pytest.raises(Boom, match="kaboom"):
        async for f in wrapped():
            got.append(f)
    assert got == ["a"], "frames before the exception must be preserved"


@pytest.mark.asyncio
async def test_cancellation_propagates():
    async def inner():
        yield "a"
        await asyncio.sleep(10)

    async def wrapped():
        with td.turn_scope(conversation_id="c1"):
            async for f in inner():
                yield f

    agen = wrapped()
    assert await agen.__anext__() == "a"
    task = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── 4. identity semantics ────────────────────────────────────────────────
def test_turn_id_stable_within_scope_unique_across_scopes_and_restored():
    outer = td.current_turn_id()
    with td.turn_scope(conversation_id="c1") as t1:
        assert td.current_turn_id() == t1
        with td.turn_scope(conversation_id="c1") as t2:
            assert t2 != t1, "nested scope must mint a distinct turn id"
        assert td.current_turn_id() == t1, "nested scope must restore the outer id"
    # Leaving the scope must not leave the inner id installed.
    assert td.current_turn_id() not in (t1, t2)
    assert outer != t1


def test_turn_id_restored_even_on_exception():
    with pytest.raises(RuntimeError):
        with td.turn_scope(conversation_id="c1") as t_in:
            raise RuntimeError("fail")
    # A fresh call must mint a NEW id, not reuse the failed turn's.
    assert td.current_turn_id() != t_in


# ── 5. thread propagation (FSM path) ─────────────────────────────────────
@pytest.mark.asyncio
async def test_turn_id_propagates_through_to_thread():
    with td.turn_scope(conversation_id="c1") as t:
        seen = await asyncio.to_thread(td.current_turn_id)
    assert seen == t


# ── 6. counters ──────────────────────────────────────────────────────────
def test_counters_record_observations():
    td.reset_counters()
    td.note_guard("promise_strip", action="regenerated", before_len=120, after_len=0)
    td.note_guard("promise_strip", action="regenerated", before_len=80, after_len=0)
    td.note_terminal("done", content_len=100)
    snap = td.snapshot_counters()
    assert snap.get("guard.promise_strip") == 2
    assert snap.get("guard_action.regenerated") == 2
    assert snap.get("terminal.done") == 1


# ── 7. privacy: no answer text in logs ───────────────────────────────────
def test_logs_contain_lengths_not_answer_text(caplog):
    secret = "CONFIDENTIAL-CUSTOMER-REVENUE-1234567890"
    with caplog.at_level(logging.INFO, logger="app.services.turn_diagnostics"):
        td.note_guard("duplicate_tail", action="stripped",
                      before_len=len(secret), after_len=len(secret) // 2)
        td.note_terminal("done", content_len=len(secret))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in text, "diagnostics must never log answer text"
    assert str(len(secret)) in text, "lengths are the intended payload"
