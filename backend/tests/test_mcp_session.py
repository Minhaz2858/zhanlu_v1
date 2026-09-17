"""MCP session manager tests — creation race + health-check reconnect.

2026-09-16: added alongside the fix for two real defects:

* ``get()`` released its lock *before* awaiting the connection factory, so
  concurrent first-use calls each spawned a server and the loser leaked
  (stdio servers are real subprocesses).
* staleness was TTL-only, so a session whose server had died was still handed
  to an agent and surfaced as a tool error instead of being rebuilt.
"""

import asyncio

import pytest

from app.services.mcp_client.session import McpSessionManager


class _StubConn:
    def __init__(self, alive: bool = True):
        self.alive = alive
        self.closed = False
        self.pings = 0

    async def ping(self, timeout_s=None):
        self.pings += 1
        return self.alive

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_concurrent_get_creates_one_connection():
    """Five concurrent first-use calls must spawn exactly one server."""
    mgr = McpSessionManager(ttl_seconds=60)
    made = []

    async def factory(*_a):
        await asyncio.sleep(0.05)  # widen the race window
        conn = _StubConn()
        made.append(conn)
        return conn

    results = await asyncio.gather(*(mgr.get("s1", factory) for _ in range(5)))

    assert len(made) == 1, "factory ran more than once — duplicate connection leaked"
    assert len({id(r) for r in results}) == 1


@pytest.mark.asyncio
async def test_get_healthy_rebuilds_a_dead_session():
    mgr = McpSessionManager(ttl_seconds=60)
    made = []

    async def factory(*_a):
        # First connection is dead, the replacement is alive.
        conn = _StubConn(alive=bool(made))
        made.append(conn)
        return conn

    conn = await mgr.get_healthy("s1", factory)

    assert conn.alive is True
    assert len(made) == 2, "dead session was not rebuilt"
    assert made[0].closed is True


@pytest.mark.asyncio
async def test_get_healthy_reuses_a_live_session():
    mgr = McpSessionManager(ttl_seconds=60)
    made = []

    async def factory(*_a):
        conn = _StubConn(alive=True)
        made.append(conn)
        return conn

    first = await mgr.get_healthy("s1", factory)
    second = await mgr.get_healthy("s1", factory)

    assert first is second
    assert len(made) == 1


@pytest.mark.asyncio
async def test_stale_session_is_evicted_and_closed():
    # Negative TTL: every entry is stale the moment it is stored.
    mgr = McpSessionManager(ttl_seconds=-1)
    made = []

    async def factory(*_a):
        conn = _StubConn()
        made.append(conn)
        return conn

    first = await mgr.get("s1", factory)
    second = await mgr.get("s1", factory)

    assert first is not second
    assert len(made) == 2
    assert first.closed is True


@pytest.mark.asyncio
async def test_close_all_closes_every_tracked_connection():
    mgr = McpSessionManager(ttl_seconds=60)
    made = []

    async def factory(name):
        conn = _StubConn()
        made.append(conn)
        return conn

    await mgr.get("a", factory, "a")
    await mgr.get("b", factory, "b")
    await mgr.close_all()

    assert len(made) == 2
    assert all(c.closed for c in made)
