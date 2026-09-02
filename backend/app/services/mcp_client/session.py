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

from app.services.mcp_client import McpConnection

logger = logging.getLogger(__name__)

# Pluggable factory so tests can inject fakes: (db, server_row) -> McpConnection
ConnectionFactory = Callable[..., Awaitable[McpConnection]]

DEFAULT_TTL_SECONDS = 600.0


class McpSessionManager:
    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._lock = asyncio.Lock()
        self._connections: dict[str, McpConnection] = {}
        self._created_at: dict[str, float] = {}
        self._ttl = ttl_seconds

    def _is_stale(self, server_id: str) -> bool:
        return (time.monotonic() - self._created_at.get(server_id, 0.0)) > self._ttl

    async def get(
        self,
        server_id: str,
        factory: ConnectionFactory,
        *factory_args: Any,
    ) -> McpConnection:
        """Return a cached (or freshly created) connection for a server."""
        async with self._lock:
            existing = self._connections.get(server_id)
            if existing is not None and not self._is_stale(server_id):
                return existing
            if existing is not None:
                await self._safe_close(existing)
                self._connections.pop(server_id, None)
                self._created_at.pop(server_id, None)

        conn = await factory(*factory_args)
        async with self._lock:
            self._connections[server_id] = conn
            self._created_at[server_id] = time.monotonic()
        return conn

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
