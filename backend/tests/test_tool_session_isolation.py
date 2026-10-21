"""Regression tests for audit P1 — shared SQLAlchemy session across parallel
tool paths.

A single Session is not concurrency-safe: the agent loop fans out parallel
tool calls with ``asyncio.gather`` and wraps sync handlers in
``asyncio.to_thread``. Sharing one session races the connection and can
interleave transactions. ``app.database.tool_session`` yields an isolated
session per tool call; these tests lock in that isolation.
"""
import asyncio

import app.database as database
from app.database import tool_session


class _RecordingSession:
    """Fake session that records lifecycle calls for assertions."""

    def __init__(self):
        self.committed = False
        self.rolled = False
        self.closed = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled = True

    def close(self):
        self.closed = True


def _patch_session(monkeypatch, log):
    database.SessionLocal = lambda: _RecordingSession()


def test_tool_session_closes_on_normal_exit(monkeypatch):
    log = []
    monkeypatch.setattr(database, "SessionLocal", lambda: _RecordingSession())
    with tool_session() as s:
        assert isinstance(s, _RecordingSession)
    assert s.closed is True
    assert s.committed is True
    assert s.rolled is False


def test_tool_session_rolls_back_on_error(monkeypatch):
    monkeypatch.setattr(database, "SessionLocal", lambda: _RecordingSession())
    s = None
    try:
        with tool_session() as _s:
            s = _s
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert s is not None
    assert s.rolled is True
    assert s.closed is True
    assert s.committed is False


def test_concurrent_tool_calls_get_distinct_sessions(monkeypatch):
    created = []

    def factory():
        sess = _RecordingSession()
        created.append(sess)
        return sess

    monkeypatch.setattr(database, "SessionLocal", factory)

    async def invoke(name, args):
        # mirrors how the router/delegate wrap each parallel tool call
        with tool_session() as s:
            return {"success": True, "name": name}

    async def run():
        return await asyncio.gather(invoke("a", {}), invoke("b", {}))

    results = asyncio.run(run())

    # Two concurrent calls => two distinct sessions, each committed + closed.
    assert len(results) == 2
    assert len(created) == 2
    assert created[0] is not created[1]
    assert all(s.committed and s.closed for s in created)
