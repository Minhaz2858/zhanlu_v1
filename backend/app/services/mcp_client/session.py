"""MCP session manager — caches live McpConnection instances per server.

Connections are created on first use from a ``McpServer`` row, kept for
``MCP_SESSION_TTL_SECONDS``, and closed on disconnect / server stop. All
methods are best-effort: a stale session is dropped and recreated.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from app.config import settings
from app.services.mcp_client import McpConnection

logger = logging.getLogger(__name__)

# Pluggable factory so tests can inject fakes: (db, server_row) -> McpConnection
ConnectionFactory = Callable[..., Awaitable[McpConnection]]

DEFAULT_TTL_SECONDS = 600.0


class McpSessionManager:
    def __init__(self, ttl_seconds: float | None = None) -> None:
        self._lock = asyncio.Lock()
        self._connections: dict[str, McpConnection] = {}
        self._created_at: dict[str, float] = {}
        # Per-server creation locks. Held only while a connection is being
        # built, so a slow server cannot block unrelated servers.
        self._creation_locks: dict[str, asyncio.Lock] = {}
        if ttl_seconds is None:
            ttl_seconds = float(
                getattr(settings, "MCP_SESSION_TTL_S", DEFAULT_TTL_SECONDS)
                or DEFAULT_TTL_SECONDS
            )
        self._ttl = ttl_seconds

    def _is_stale(self, server_id: str) -> bool:
        return (time.monotonic() - self._created_at.get(server_id, 0.0)) > self._ttl

    async def _peek(self, server_id: str) -> McpConnection | None:
        """Return a live cached connection, evicting (and closing) a stale one."""
        async with self._lock:
            conn = self._connections.get(server_id)
            if conn is None:
                return None
            if not self._is_stale(server_id):
                return conn
            self._connections.pop(server_id, None)
            self._created_at.pop(server_id, None)
        await self._safe_close(conn)
        return None

    async def get(
        self,
        server_id: str,
        factory: ConnectionFactory,
        *factory_args: Any,
    ) -> McpConnection:
        """Return a cached (or freshly created) connection for a server.

        2026-09-16: the global lock used to be released *before* awaiting the
        factory, so two concurrent first-use calls both missed the cache, both
        spawned a connection, and the second dict write silently leaked the
        first — stdio servers are real subprocesses, so that leaked a process
        per race. Creation is now serialized per server with a double-checked
        cache read.
        """
        cached = await self._peek(server_id)
        if cached is not None:
            return cached

        creation_lock = self._creation_locks.setdefault(server_id, asyncio.Lock())
        async with creation_lock:
            # Another caller may have won the race while we waited.
            cached = await self._peek(server_id)
            if cached is not None:
                return cached

            conn = await factory(*factory_args)
            async with self._lock:
                self._connections[server_id] = conn
                self._created_at[server_id] = time.monotonic()
            return conn

    async def get_healthy(
        self,
        server_id: str,
        factory: ConnectionFactory,
        *factory_args: Any,
        health_timeout_s: float | None = None,
    ) -> McpConnection:
        """Like :meth:`get`, but verify the connection answers before use.

        2026-09-16: staleness was TTL-only, so a session whose server had died
        (crash, dropped pipe, restarted container) was still handed to an
        agent and surfaced as a tool error. A failed ping now closes the
        session and rebuilds it once.
        """
        conn = await self.get(server_id, factory, *factory_args)
        if await conn.ping(health_timeout_s):
            return conn
        logger.info(
            "MCP session %s failed health check; reconnecting", server_id,
        )
        await self.close(server_id)
        return await self.get(server_id, factory, *factory_args)

    async def close(self, server_id: str) -> None:
        async with self._lock:
            conn = self._connections.pop(server_id, None)
            self._created_at.pop(server_id, None)
        if conn is not None:
            await self._safe_close(conn)

    async def close_all(self) -> None:
        async with self._lock:
            conns = list(self._connections.values())
            self._connections.clear()
            self._created_at.clear()
        for conn in conns:
            await self._safe_close(conn)

    async def _safe_close(self, conn: McpConnection) -> None:
        try:
            await conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("MCP session close failed: %s", exc)


_sessions = McpSessionManager()


def get_session_manager() -> McpSessionManager:
    return _sessions


__all__ = ["McpSessionManager", "get_session_manager"]
