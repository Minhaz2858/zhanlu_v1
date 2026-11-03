"""Phase 9 reliability (audit A1): tool dispatch must not share the caller's
SQLAlchemy Session across worker threads.

``_execute_with_reliability`` dispatches SYNC tool handlers via
``asyncio.to_thread`` while parallel batches run several ``call_fn``
coroutines concurrently. Handing the caller's single Session to worker
threads corrupts the session's connection/transaction state (intermittent
InvalidRequestError, then misclassified as retryable). Each sync handler
call must receive its OWN Session (fresh ``SessionLocal()``), closed after
the call — never the caller's session object.
"""
import asyncio

import pytest

from app.services.agent_tools import _execute_with_reliability


@pytest.mark.asyncio
async def test_sync_handler_gets_fresh_session_not_callers(monkeypatch):
    """A sync handler dispatched to a thread must receive a Session that is
    NOT the caller's session object."""
    from app.services import agent_tools

    seen = {}
    caller_db = object()  # sentinel: caller's session

    class _FakeSession:
        def close(self):
            pass

    monkeypatch.setattr(agent_tools, "SessionLocal", _FakeSession, raising=False)

    def sync_handler(args, db, user_id):
        seen["db"] = db
        return {"success": True}

    result = await _execute_with_reliability(
        "fake_sync_tool", {"a": 1}, sync_handler, caller_db, "u1",
        context=None, use_context=False,
    )
    assert result["success"] is True
    assert seen["db"] is not caller_db, (
        "sync handler received the caller's Session — thread-unsafe dispatch"
    )


@pytest.mark.asyncio
async def test_parallel_sync_handlers_get_distinct_sessions(monkeypatch):
    """Two sync handlers in one parallel batch must each get a DIFFERENT
    session (the historical corruption: one shared Session used by two
    threads at once)."""
    from app.services import agent_tools

    seen = []
    caller_db = object()
    counter = {"n": 0}

    def fake_sessionlocal():
        counter["n"] += 1

        class _S:
            n = counter["n"]
        return _S()

    monkeypatch.setattr(agent_tools, "SessionLocal", fake_sessionlocal, raising=False)

    def make_handler(idx):
        def handler(args, db, user_id):
            seen.append((idx, db))
            return {"success": True}
        return handler

    # Simulate the parallel-batch call pattern: both call sites enter the
    # dispatch concurrently (as _exec_v2_sibling / _execute_batch do).
    await asyncio.gather(
        _execute_with_reliability("t1", {}, make_handler(1), caller_db, "u1",
                                  context=None, use_context=False),
        _execute_with_reliability("t2", {}, make_handler(2), caller_db, "u1",
                                  context=None, use_context=False),
    )

    dbs = [db for _, db in seen]
    assert len(dbs) == 2
    assert dbs[0] is not dbs[1], "two concurrent sync handlers shared ONE session"
    assert all(db is not caller_db for db in dbs)


@pytest.mark.asyncio
async def test_sync_handler_session_closed_after_call(monkeypatch):
    """The per-call session must be closed even when the handler raises —
    no connection leaks on the failure path."""
    from app.services import agent_tools

    closed = []

    class _FakeSession:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(agent_tools, "SessionLocal",
                        lambda: _FakeSession(), raising=False)

    def boom(args, db, user_id):
        raise RuntimeError("handler exploded")

    result = await _execute_with_reliability(
        "boom_tool", {}, boom, object(), "u1",
        context=None, use_context=False,
    )
    assert result.get("success") is False
    assert closed == [True], "per-call session was not closed on failure"


@pytest.mark.asyncio
async def test_registry_level_sync_tool_uses_own_session(monkeypatch):
    """Integration through execute_tool: a registry-registered sync handler
    must not observe the caller's session."""
    from app.services import agent_tools
    from app.services.tool_registry import registry

    seen = {}
    caller_db = object()

    class _FakeSession:
        def close(self):
            pass

    monkeypatch.setattr(agent_tools, "SessionLocal", _FakeSession, raising=False)

    def sync_handler(args, db, user_id, context=None):
        seen["db"] = db
        return {"success": True}

    monkeypatch.setattr(registry, "get_handler",
                        lambda name: sync_handler if name == "fake_iso_tool" else None)

    result = await agent_tools.execute_tool(
        "fake_iso_tool", {}, caller_db, "u1",
    )
    assert result.get("success") is True
    assert seen["db"] is not caller_db
