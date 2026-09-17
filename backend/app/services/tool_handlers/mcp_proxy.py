"""MCP proxy — forwards tool calls to connected MCP servers (SDK path).

2026-09-16 unification
---------------------
This module used to carry a *second*, hand-rolled stdio JSON-RPC client
(``probe_mcp_server`` / ``list_tools_for_server`` / ``call_mcp_tool``) that
skipped the MCP handshake entirely: it wrote ``tools/list`` as the very first
frame and closed stdin immediately afterwards via ``communicate()``. A
spec-compliant server requires ``initialize`` → ``notifications/initialized``
before it will answer anything, so that path could not talk to a real server —
while the correct SDK client sat right next door in
``app.services.mcp_client`` and was used by the *other* half of the router.

Everything now goes through the official SDK client: protocol negotiation,
``stdio`` and ``streamable-http`` transports, and a health-checked, cached
session per server (see :mod:`app.services.mcp_client.session`).

Two entry points, because two kinds of caller exist:

* :func:`call_mcp_tool_for_server` — async, reuses the cached session. This is
  the path for agents and request handlers.
* :func:`call_mcp_tool` — sync bridge for ``workflow_runtime.mcp_call``, which
  is a sync method on a sync class. It uses a *throwaway* connection on purpose:
  MCP session objects are bound to the event loop that created them, so a
  cached session cannot safely be awaited from a different loop.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.services.mcp_client import _split_command  # noqa: F401  (re-export)

logger = logging.getLogger(__name__)

# Last-known tool list per server *name*, for the UI/workflow tool picker.
# The live connection itself is owned by McpSessionManager — this is metadata
# only, refreshed by the probe in app/routers/mcp.py.
_mcp_tools_cache: dict[str, list[dict[str, Any]]] = {}


def register_mcp_tools(server_name: str, tools: list[dict[str, Any]]) -> None:
    """Cache a server's tool list (called by the router's probe)."""
    _mcp_tools_cache[server_name] = tools


def get_mcp_tools_for_server(server_name: str) -> list[dict[str, Any]]:
    """Cached tool list for a connected MCP server (empty when unknown)."""
    return _mcp_tools_cache.get(server_name, [])


def _call_timeout() -> float:
    try:
        from app.config import settings

        return float(getattr(settings, "MCP_CALL_TIMEOUT_S", 60.0) or 60.0)
    except Exception:  # noqa: BLE001
        return 60.0


# ---------------------------------------------------------------------------
# Async path — cached, health-checked session
# ---------------------------------------------------------------------------

async def call_mcp_tool_for_server(
    server,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Call a tool on ``server`` over a cached, health-checked session.

    ``server`` is a ``McpServer`` row. Returns the normalized Zhanlu tool
    result (``{"success": bool, ...}``) — never raises for server-side faults,
    so the agent loop sees a failed tool call rather than a broken turn.
    """
    from app.services.mcp_client import connection_for_server
    from app.services.mcp_client.session import get_session_manager

    manager = get_session_manager()
    try:
        conn = await manager.get_healthy(server.id, connection_for_server, server)
        return await conn.call_tool(tool_name, arguments or {}, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001
        # A session that failed to build or call must not keep poisoning
        # later calls — drop it so the next attempt reconnects.
        try:
            await manager.close(server.id)
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "MCP call failed (%s / %s): %s",
            getattr(server, "name", "?"), tool_name, exc,
        )
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Sync path — ephemeral connection, loop-safe
# ---------------------------------------------------------------------------

async def _call_ephemeral(
    server, tool_name: str, arguments: dict[str, Any], timeout_s: float,
) -> dict[str, Any]:
    """Open a throwaway connection, call, close. Safe from any event loop."""
    from app.services.mcp_client import connection_for_server

    conn = await connection_for_server(server)
    try:
        return await conn.call_tool(tool_name, arguments or {}, timeout_s=timeout_s)
    finally:
        await conn.close()


def call_mcp_tool(
    server,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Sync bridge for non-async callers (``workflow_runtime.mcp_call``).

    Takes the ``McpServer`` row, not a URL string: the previous signature took
    ``server_url`` and re-parsed it with ``shlex``, which quietly broke HTTP
    transports (a URL is not a command) and re-spawned a process per call.
    """
    if timeout_s is None:
        timeout_s = _call_timeout()
    args = arguments if isinstance(arguments, dict) else {}

    def _runner() -> dict[str, Any]:
        return asyncio.run(_call_ephemeral(server, tool_name, args, timeout_s))

    def _fail(exc: Exception) -> dict[str, Any]:
        logger.warning("MCP sync call failed (%s / %s): %s", tool_name, exc, exc)
        return {"success": False, "error": str(exc)}

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop in this thread — run directly.
        try:
            return _runner()
        except Exception as exc:  # noqa: BLE001
            return _fail(exc)

    # We are inside a running loop (a FastAPI request). The SDK is async-only
    # and its sessions are loop-bound, so drive a private loop in a worker
    # thread instead of touching the caller's loop.
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_runner).result(timeout=timeout_s + 30.0)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ---------------------------------------------------------------------------
# Agent-facing handler
# ---------------------------------------------------------------------------

def _lookup_server(db, server_name: str):
    """Find a live McpServer row by name (own session when db is absent)."""
    try:
        from app.models.mcp_server import McpServer
    except Exception:  # noqa: BLE001
        return None

    query = lambda session: (  # noqa: E731
        session.query(McpServer)
        .filter(McpServer.name == server_name, McpServer.is_deleted == False)  # noqa: E712
        .first()
    )

    if db is not None:
        try:
            return query(db)
        except Exception:  # noqa: BLE001
            return None

    try:
        from app.database import SessionLocal

        session = SessionLocal()
    except Exception:  # noqa: BLE001
        return None
    try:
        return query(session)
    except Exception:  # noqa: BLE001
        return None
    finally:
        session.close()


async def _mcp_proxy_handler(
    args: dict,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Forward a tool call to the MCP server that owns the tool.

    2026-09-16: this was a stub that always returned "not yet implemented",
    so any agent granted an MCP tool got a guaranteed failure. It now performs
    the forward over the SDK session.
    """
    ctx = context or {}
    tool_name = ctx.get("tool_name")
    if not tool_name:
        return {"success": False, "error": "MCP proxy: no tool_name in context"}

    server_name = ctx.get("mcp_server")
    if not server_name:
        return {"success": False, "error": "MCP proxy: no mcp_server in context"}

    server = _lookup_server(db, server_name)
    if server is None:
        return {
            "success": False,
            "error": (
                f"MCP proxy: server '{server_name}' is not connected"
            ),
        }

    arguments = args if isinstance(args, dict) else {}
    return await call_mcp_tool_for_server(server, tool_name, arguments)
