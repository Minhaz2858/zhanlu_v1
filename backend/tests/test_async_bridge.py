"""Sync-to-async bridging, and the FSM FINALIZE bug it fixes.

The bug these tests pin (found live 2026-09-18): every *blocking* FINALIZE in
``SynexiaFSM`` answered the user "I've processed your request." while the LLM was
never called. Root cause was a bridge that assumed it ran in a worker thread:

    import asyncio as _asyncio
    if _asyncio.iscoroutine(result):
        _loop = _asyncio.new_event_loop()
        result = _loop.run_until_complete(result)   # RuntimeError here

``SynexiaFSM.run`` is sync and the chat router calls it directly from
``async def add_message`` (agents.py:6676), so a loop was already running on that
thread and nesting one raised:

    RuntimeError: Cannot run the event loop while another loop is running

The ``except`` above it then produced the same contentless reply, so the failure
was indistinguishable from success in the logs (WARNING) and invisible to the user.

The first test below is the one that would have caught it: an async LLM call behind
a sync FSM method, invoked from a running loop.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.async_bridge import loop_is_running, run_coro_blocking


# ── the bridge itself ────────────────────────────────────────────────────────

def test_no_loop_running_uses_plain_asyncio_run():
    assert loop_is_running() is False

    async def value():
        return 42

    assert run_coro_blocking(value()) == 42


def test_live_loop_does_not_raise_the_nesting_error():
    """The exact failure mode: sync code invoked from inside a running loop.
    ``asyncio.run`` here would raise 'Cannot run the event loop while another loop
    is running'."""
    async def scenario():
        assert loop_is_running() is True

        async def value():
            return "ok"

        return run_coro_blocking(value())

    assert asyncio.run(scenario()) == "ok"


def test_live_loop_branch_actually_executes_the_coroutine():
    """Guard against a 'fix' that swallows the coroutine instead of driving it."""
    ran: list[str] = []

    async def work():
        ran.append("ran")
        return "done"

    async def scenario():
        return run_coro_blocking(work())

    assert asyncio.run(scenario()) == "done"
    assert ran == ["ran"]


def test_exceptions_propagate_and_are_not_rewritten():
    """A genuine failure must reach the caller. The FSM decides how to report it;
    the bridge must not turn it into a success-shaped value."""
    async def boom():
        raise ValueError("provider refused")

    def sync_context():
        return run_coro_blocking(boom())

    with pytest.raises(ValueError, match="provider refused"):
        sync_context()

    async def scenario():
        return run_coro_blocking(boom())

    with pytest.raises(ValueError, match="provider refused"):
        asyncio.run(scenario())


def test_works_from_a_worker_thread_with_a_live_loop_on_the_main_thread():
    """Both contexts at once: a worker thread that has no loop of its own, while
    the test's main thread does — the branch must not depend on the caller."""
    async def work():
        return "from-worker"

    def worker():
        return run_coro_blocking(work())

    async def scenario():
        import anyio
        return await anyio.to_thread.run_sync(worker)

    assert asyncio.run(scenario()) == "from-worker"


# ── the FSM regression ───────────────────────────────────────────────────────

@pytest.fixture
def fsm(monkeypatch):
    """A SynexiaFSM with only the response prompt stubbed out — the LLM call and
    the bridge are what these tests exercise."""
    from app.services.synexia import fsm as fsm_mod

    machine = fsm_mod.SynexiaFSM(db=None)
    monkeypatch.setattr(machine, "_build_response_prompt", lambda request: "prompt")
    return machine, fsm_mod


def test_fsm_blocking_finalize_calls_the_llm_when_a_loop_is_running(fsm, monkeypatch):
    """The live bug, as a test: an ASYNC call_llm behind the sync FINALIZE path,
    reached from a running loop. Before the fix this returned the canned string."""
    machine, fsm_mod = fsm
    from app.services import llm_service

    async def fake_call_llm(**kwargs):
        return {"response": "FAMOUNT 1,234,567 for last month"}

    monkeypatch.setattr(llm_service, "call_llm", fake_call_llm)
    request = fsm_mod.ExecutionRequest(
        conversation_id="conv-bridge-test", user_message="last month's contract performance"
    )

    async def scenario():
        return machine._generate_response(request)

    answer = asyncio.run(scenario())
    assert answer == "FAMOUNT 1,234,567 for last month"
    assert answer != "I've processed your request."


def test_fsm_sync_stub_is_passed_through_untouched(fsm, monkeypatch):
    """Tests and some callers inject a SYNC stub returning a dict; the bridge must
    not interfere with a non-coroutine result."""
    machine, fsm_mod = fsm
    from app.services import llm_service

    monkeypatch.setattr(llm_service, "call_llm", lambda **kwargs: {"response": "sync stub"})
    request = fsm_mod.ExecutionRequest(user_message="hi")

    assert machine._generate_response(request) == "sync stub"


def test_fsm_genuine_llm_failure_is_logged_at_error_not_warning(fsm, monkeypatch, caplog):
    """When the LLM really does fail the request, that must be visible in ops.

    This documents a remaining hazard rather than blessing it: the user still gets
    a contentless reply instead of an error state. The text was deliberately NOT
    reworded — a prettier failure message would hide the problem, not fix it.
    """
    import logging

    machine, fsm_mod = fsm
    from app.services import llm_service

    async def broken_call_llm(**kwargs):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(llm_service, "call_llm", broken_call_llm)
    request = fsm_mod.ExecutionRequest(conversation_id="conv-x", user_message="hi")

    with caplog.at_level(logging.ERROR):
        answer = machine._generate_response(request)

    assert answer == "I've processed your request."
    assert any(
        record.levelno >= logging.ERROR and "conv-x" in record.getMessage()
        for record in caplog.records
    ), "a failed FINALIZE must be reported at ERROR with the conversation id"
