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
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from app.services.mcp_client import _split_command  # noqa: F401  (re-export)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-tool registration
# ---------------------------------------------------------------------------
#
# One registry entry per (server, tool), named ``mcp__<server>__<tool>``.
#
# Why one entry per tool rather than one ``mcp`` tool taking a server/tool pair
# as arguments: the model picks tools by name and schema. A generic dispatcher
# forces it to hallucinate a server name and a tool name from prose, and hides
# the real argument schema from the tool list — so the call was always one
# guess away from a 404. Registering each server tool with its own name and its
# own JSON schema makes an MCP tool indistinguishable from a built-in one.
#
# Routing is carried by the handler closure (server id + original tool name),
# not by request context: context is assembled per call site, so anything not
# injected at all 12 `execute_tool(...)` call sites silently routes nowhere.

_SEPARATOR = "__"
_MAX_TOOL_NAME = 64  # OpenAI/DeepSeek reject longer function names
_SLUG_RE = re.compile(r"[^0-9a-zA-Z]+")


def _slug(value: str) -> str:
    """"My Server/2.0" -> "my_server_2_0" (safe in a function name)."""
    slug = _SLUG_RE.sub("_", (value or "").strip()).strip("_").lower()
    return slug or "unnamed"


def mcp_registry_name(server_name: str, tool_name: str) -> str:
    """Deterministic registry name for a server's tool.

    Truncated to the provider's 64-char function-name limit, preserving a
    hash of the full pair so truncation can never alias two different tools.
    """
    base = f"mcp{_SEPARATOR}{_slug(server_name)}{_SEPARATOR}{_slug(tool_name)}"
    if len(base) <= _MAX_TOOL_NAME:
        return base
    import hashlib

    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    return f"{base[: _MAX_TOOL_NAME - 9]}_{digest}"


@dataclass
class RegisteredMcpServer:
    """In-memory record of one probed MCP server and its registry entries."""

    server_id: str
    server_name: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    registry_names: list[str] = field(default_factory=list)
    granted_agents: list[str] = field(default_factory=list)

    def grants(self, agent_name: str | None) -> bool:
        """True when ``agent_name`` may use this server's tools.

        An empty grant list means *every* agent: connecting a server is already
        an explicit operator action, and requiring a second per-agent opt-in is
        how these tools ended up unreachable in the first place.
        """
        if not self.granted_agents:
            return True
        if not agent_name:
            return False
        return agent_name in self.granted_agents

    def tool_dict(self, tool_name: str) -> dict[str, Any] | None:
        for t in self.tools:
            if t.get("name") == tool_name:
                return t
        return None


_MCP_SERVERS: dict[str, RegisteredMcpServer] = {}


def _mcp_tools_enabled() -> bool:
    try:
        from app.config import settings

        return bool(getattr(settings, "MCP_TOOLS_ENABLED", True))
    except Exception:  # noqa: BLE001
        return True


def _find_server(server_ref: Any) -> RegisteredMcpServer | None:
    """Look a registration up by server id, falling back to server name."""
    key = str(server_ref)
    record = _MCP_SERVERS.get(key)
    if record is not None:
        return record
    for candidate in _MCP_SERVERS.values():
        if candidate.server_name == key:
            return candidate
    return None


def _make_forwarder(server_id: str, tool_name: str, server_name: str):
    """Build the handler for one MCP tool — routing closed over, not looked up."""

    async def _forward(
        args: dict,
        db=None,
        user_id: str | None = None,
        context: dict | None = None,
    ) -> dict:
        server = _lookup_server_by_id(db, server_id)
        if server is None:
            return {
                "success": False,
                "error": (
                    f"The MCP server '{server_name}' is no longer connected, so "
                    f"'{tool_name}' cannot be called. Reconnect it to continue."
                ),
            }
        return await call_mcp_tool_for_server(
            server, tool_name, args if isinstance(args, dict) else {},
        )

    _forward.__name__ = f"mcp_forward_{_slug(server_name)}_{_slug(tool_name)}"
    return _forward


def _tool_schema(record: RegisteredMcpServer, tool: dict[str, Any], reg_name: str) -> dict:
    """OpenAI-format schema for one MCP tool."""
    from app.services.tool_registry import normalize_tool_schema

    params = tool.get("inputSchema") or tool.get("input_schema") or {}
    if not isinstance(params, dict) or not params:
        params = {"type": "object", "properties": {}}
    description = (tool.get("description") or "").strip()
    return normalize_tool_schema({
        "name": reg_name,
        "description": f"[{record.server_name}] {description}".strip(),
        "parameters": params,
    }, fallback_name=reg_name)


def register_mcp_tools(
    server: Any,
    tools: list[dict[str, Any]],
    granted_agents: list[str] | None = None,
) -> RegisteredMcpServer:
    """Publish a probed server's tools as agent-callable registry tools.

    ``server`` is an ``McpServer`` row (preferred — gives a stable id) or a
    plain name string. Idempotent: re-probing the same server refreshes the
    schemas in place and deregisters tools the server no longer exposes.

    Returns the registration record (used by the router to expose registry
    names to the UI).
    """
    from app.services.tool_registry import registry

    server_id = str(getattr(server, "id", None) or server)
    server_name = str(getattr(server, "name", None) or server)
    if granted_agents is None:
        granted_agents = list(getattr(server, "granted_agents", None) or [])

    previous = _MCP_SERVERS.get(server_id)
    previous_names = set(previous.registry_names) if previous else set()
    previous_tools = {t.get("name"): t for t in (previous.tools if previous else [])}

    record = RegisteredMcpServer(
        server_id=server_id,
        server_name=server_name,
        tools=list(tools or []),
        granted_agents=list(granted_agents or []),
    )

    seen_names: set[str] = set()
    for tool in record.tools:
        tool_name = tool.get("name")
        if not tool_name:
            continue
        reg_name = mcp_registry_name(server_name, tool_name)
        # Namespace collisions: a different (server, tool) already holding this
        # name would otherwise have its handler silently replaced.
        if reg_name in seen_names or (
            reg_name in previous_names and reg_name not in _names_for(previous, tool_name)
        ):
            import hashlib

            suffix = hashlib.sha1(f"{server_id}:{tool_name}".encode("utf-8")).hexdigest()[:6]
            reg_name = f"{reg_name[: _MAX_TOOL_NAME - 7]}_{suffix}"
        seen_names.add(reg_name)
        registry.register(
            name=reg_name,
            schema=_tool_schema(record, tool, reg_name),
            handler=_make_forwarder(server_id, tool_name, server_name),
            category="mcp",
            toolset=f"mcp:{_slug(server_name)}",
            enabled_by_default=True,
            description=(tool.get("description") or f"{tool_name} on {server_name}"),
            emoji="🔌",
            max_result_size_chars=20000,
        )
        record.registry_names.append(reg_name)

    # Drop tools the server stopped advertising (or removed tools on disconnect).
    for stale in previous_names - set(record.registry_names):
        registry.deregister(stale)

    _MCP_SERVERS[server_id] = record
    logger.info(
        "MCP server '%s': %d tool(s) published to the agent registry (%s)",
        server_name, len(record.registry_names),
        ", ".join(record.registry_names[:5]) + ("…" if len(record.registry_names) > 5 else ""),
    )
    return record


def _names_for(record: RegisteredMcpServer | None, tool_name: str) -> set[str]:
    """Registry names a previous registration gave to ``tool_name``."""
    if record is None:
        return set()
    names: set[str] = set()
    for name in record.registry_names:
        if name.startswith(mcp_registry_name(record.server_name, tool_name)):
            names.add(name)
    return names


def unregister_mcp_server(server: Any) -> None:
    """Withdraw every tool a server published (disconnect / delete)."""
    from app.services.tool_registry import registry

    record = _find_server(getattr(server, "id", None) or server)
    if record is None:
        return
    for name in record.registry_names:
        registry.deregister(name)
    _MCP_SERVERS.pop(record.server_id, None)
    logger.info("MCP server '%s' withdrew %d tool(s)", record.server_name, len(record.registry_names))


def get_mcp_tools_for_server(server_ref: Any) -> list[dict[str, Any]]:
    """Cached tool list for a probed MCP server (empty when unknown)."""
    record = _find_server(server_ref)
    if record is None:
        return []
    return [
        {**tool, "registry_name": name}
        for tool, name in zip(record.tools, record.registry_names)
    ]


def registered_servers() -> list[RegisteredMcpServer]:
    """Snapshot of every server currently published to the tool registry."""
    return list(_MCP_SERVERS.values())


def mcp_tools_for_agent(agent_name: str | None) -> list[dict[str, Any]]:
    """Tool schemas the given agent is granted, from connected MCP servers.

    Returns an empty list when MCP tool publishing is disabled. Grants are read
    from the in-memory registration (refreshed on probe), so this stays off the
    DB and safe to call on every LLM turn.
    """
    if not _mcp_tools_enabled():
        return []
    from app.services.tool_registry import registry

    schemas: list[dict[str, Any]] = []
    for record in _MCP_SERVERS.values():
        if not record.grants(agent_name):
            continue
        for name in record.registry_names:
            entry = registry.get_entry(name)
            if entry is not None:
                schemas.append(entry.schema)
    return schemas


def refresh_grants(server: Any, granted_agents: list[str] | None) -> RegisteredMcpServer | None:
    """Apply a grant change made through the API without waiting for a re-probe."""
    record = _find_server(getattr(server, "id", None) or server)
    if record is None:
        return None
    record.granted_agents = list(granted_agents or [])
    logger.info(
        "MCP server '%s' access updated: %s",
        record.server_name,
        ", ".join(record.granted_agents) if record.granted_agents else "all agents",
    )
    return record


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

def _query_server(db, build_query):
    """Run ``build_query`` against ``db`` (or a short-lived own session).

    The forwarder closures resolve their server row at call time rather than
    holding the ORM object: a row can be edited, re-probed, or soft-deleted
    between registration and the call, and a stale object would either raise
    ``DetachedInstanceError`` or keep talking to a dead connection.
    """
    if db is not None:
        try:
            return build_query(db)
        except Exception:  # noqa: BLE001
            return None

    try:
        from app.database import SessionLocal

        session = SessionLocal()
    except Exception:  # noqa: BLE001
        return None
    try:
        return build_query(session)
    except Exception:  # noqa: BLE001
        return None
    finally:
        session.close()


def _lookup_server(db, server_name: str):
    """Find a live McpServer row by name (own session when db is absent)."""
    try:
        from app.models.mcp_server import McpServer
    except Exception:  # noqa: BLE001
        return None

    return _query_server(db, lambda session: (
        session.query(McpServer)
        .filter(McpServer.name == server_name, McpServer.is_deleted == False)  # noqa: E712
        .first()
    ))


def _lookup_server_by_id(db, server_id: str):
    """Find a live McpServer row by id — the route for registered MCP tools."""
    try:
        from app.models.mcp_server import McpServer
    except Exception:  # noqa: BLE001
        return None

    return _query_server(db, lambda session: (
        session.query(McpServer)
        .filter(McpServer.id == server_id, McpServer.is_deleted == False)  # noqa: E712
        .first()
    ))


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
