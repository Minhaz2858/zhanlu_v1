"""MCP proxy round-trip tests — SDK client over stdio.

2026-09-16 rewrite
------------------
These tests used to exercise a hand-rolled JSON-RPC client whose fake server
answered ``tools/list`` as the *first* frame, with no ``initialize``
handshake. That fake was far more permissive than any real MCP server, which
is exactly why the hand-rolled path passed its tests while being unable to
talk to an actual server. The fake below implements the real handshake
(``initialize`` -> ``notifications/initialized`` -> ``tools/list``), so these
tests now exercise the same protocol a production server speaks.
"""

import asyncio
import sys
import tempfile
import textwrap

import pytest

from app.services.tool_handlers.mcp_proxy import _split_command

_SERVER = r'''
import json, sys

TOOLS = [{"name": "echo", "description": "Echo back the arguments",
          "inputSchema": {"type": "object", "properties": {}}}]


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        rid = req.get("id")
        method = req.get("method")

        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "1.0.0"}}}
        elif method in ("notifications/initialized", "initialized"):
            continue  # notification — must never be answered
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {}}
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            params = req.get("params") or {}
            args = params.get("arguments") or {}
            if (params.get("name") or "") != "echo":
                resp = {"jsonrpc": "2.0", "id": rid, "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "unknown tool"}]}}
            else:
                resp = {"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps(args)}]}}
        else:
            resp = {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": "unknown method"}}

        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


main()
'''

# A server that accepts the handshake and then never answers tools/call —
# used to prove the call timeout actually bounds the agent loop.
_SILENT_SERVER = r'''
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue
    if req.get("method") == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
            "result": {"protocolVersion": "2024-11-05",
                       "capabilities": {"tools": {}},
                       "serverInfo": {"name": "silent", "version": "1"}}}) + "\n")
        sys.stdout.flush()
    elif req.get("method") in ("notifications/initialized", "initialized"):
        continue
    elif req.get("method") == "tools/list":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
            "result": {"tools": [{"name": "hang", "description": "never returns",
                                  "inputSchema": {"type": "object"}}]}}) + "\n")
        sys.stdout.flush()
    # tools/call: deliberately no response
'''


class _Row:
    """Stand-in for a McpServer ORM row (connection_for_server reads these)."""

    def __init__(self, name: str, server_url: str, transport: str = "stdio"):
        self.id = name
        self.name = name
        self.server_url = server_url
        self.base_url = None
        self.transport = transport
        self.oauth_token_json = None
        self.is_deleted = False


def _write_server(source: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(textwrap.dedent(source))
        path = f.name
    return f"{sys.executable} {path}"


def _server_url() -> str:
    return _write_server(_SERVER)


def test_split_command_parses_url():
    cmd, args = _split_command("npx -y @scope/feishu --flag")
    assert cmd == "npx"
    assert args == ["-y", "@scope/feishu", "--flag"]


def test_split_command_rejects_empty():
    with pytest.raises(ValueError):
        _split_command("")


@pytest.mark.asyncio
async def test_list_tools_through_sdk_with_handshake():
    """tools/list must work over a real initialize handshake."""
    from app.services.mcp_client import connection_for_server

    conn = await connection_for_server(_Row("fake", _server_url()))
    try:
        tools = await conn.list_tools()
        assert any(t.name == "echo" for t in tools)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_call_tool_round_trip():
    from app.services.mcp_client import connection_for_server

    conn = await connection_for_server(_Row("fake", _server_url()))
    try:
        result = await conn.call_tool("echo", {"hello": "world"})
        assert result["success"] is True
        assert "world" in result["result"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_call_tool_reports_server_side_error():
    from app.services.mcp_client import connection_for_server

    conn = await connection_for_server(_Row("fake", _server_url()))
    try:
        result = await conn.call_tool("not_a_tool", {})
        assert result["success"] is False
        assert "unknown tool" in result["error"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_call_tool_is_bounded_by_timeout():
    """A hung MCP server must fail one tool call, not the whole turn."""
    from app.services.mcp_client import connection_for_server

    conn = await connection_for_server(_Row("silent", _write_server(_SILENT_SERVER)))
    try:
        result = await conn.call_tool("hang", {}, timeout_s=1.0)
        assert result["success"] is False
        assert "timed out" in result["error"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_sync_bridge_returns_error_not_raise():
    """workflow_runtime.mcp_call is sync — the bridge must degrade cleanly."""
    from app.services.tool_handlers.mcp_proxy import call_mcp_tool

    result = call_mcp_tool(
        _Row("bad", "nonexistent_command_xyz_123"), "anything", {}
    )
    assert result["success"] is False
    assert result["error"]


@pytest.mark.asyncio
async def test_handler_requires_context():
    from app.services.tool_handlers.mcp_proxy import _mcp_proxy_handler

    assert (await _mcp_proxy_handler({}, context={}))["success"] is False
    result = await _mcp_proxy_handler({}, context={"tool_name": "x"})
    assert "no mcp_server" in result["error"].lower()


# ─── Tool publication → agent reachability (2026-09-17) ───────────────────
#
# These cover the half that was missing entirely: a probed server's tools being
# published as first-class registry tools an agent can be given and call.


def _ensure_tables():
    from app.database import engine
    from app.models.base import Base
    import app.models.mcp_server  # noqa: F401  (register the table)

    Base.metadata.create_all(engine)


def _make_row(name: str, server_url: str, **kw):
    """Persist a real McpServer row and return it (detached-safe fields)."""
    _ensure_tables()
    from app.database import SessionLocal
    from app.models.mcp_server import McpServer

    db = SessionLocal()
    row = McpServer(
        name=name, server_url=server_url, transport="stdio",
        status="connected", tools_count=0, resources_count=0, **kw,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    row_id = row.id
    db.close()
    return row_id


def _drop_row(row_id: str) -> None:
    from app.database import SessionLocal
    from app.models.mcp_server import McpServer

    db = SessionLocal()
    try:
        db.query(McpServer).filter(McpServer.id == row_id).delete()
        db.commit()
    finally:
        db.close()


def test_registry_name_is_namespaced_and_slugged():
    from app.services.tool_handlers.mcp_proxy import mcp_registry_name

    assert mcp_registry_name("My Server", "read.file") == "mcp__my_server__read_file"
    # Never empty, never punctuated — providers reject names outside [A-Za-z0-9_-].
    assert mcp_registry_name("///", "***") == "mcp__unnamed__unnamed"


def test_registry_name_truncation_never_aliases():
    from app.services.tool_handlers.mcp_proxy import mcp_registry_name

    long_a = "s" * 50
    long_b = "t" * 50
    name_a = mcp_registry_name(long_a, "search_the_whole_thing")
    name_b = mcp_registry_name(long_b, "search_the_whole_thing")
    assert len(name_a) <= 64 and len(name_b) <= 64
    assert name_a != name_b


def test_register_publishes_one_registry_tool_per_server_tool():
    from app.services.tool_handlers.mcp_proxy import (
        register_mcp_tools, unregister_mcp_server,
    )
    from app.services.tool_registry import registry

    row_id = _make_row("pub-server", "unused")
    try:
        register_mcp_tools(_IdRow(row_id, "pub-server", "unused"), [
            {"name": "search", "description": "Find things",
             "inputSchema": {"type": "object",
                             "properties": {"q": {"type": "string"}},
                             "required": ["q"]}},
            {"name": "fetch_page", "description": "Fetch", "inputSchema": {}},
        ])
        entry = registry.get_entry("mcp__pub_server__search")
        assert entry is not None, "tool was not published to the registry"
        schema = entry.schema
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "mcp__pub_server__search"
        # The server's own argument schema must survive — that is the whole
        # reason for one entry per tool instead of a generic dispatcher.
        assert schema["function"]["parameters"]["required"] == ["q"]
        # Server name is part of the description so the model can tell which
        # server a same-named tool belongs to.
        assert schema["function"]["description"].startswith("[pub-server]")
        # A tool with no schema still gets a valid empty object schema.
        empty = registry.get_entry("mcp__pub_server__fetch_page").schema
        assert empty["function"]["parameters"] == {"type": "object", "properties": {}}
    finally:
        unregister_mcp_server(row_id)
        _drop_row(row_id)
    assert registry.get_entry("mcp__pub_server__search") is None


def test_reprobe_deregisters_tools_the_server_dropped():
    from app.services.tool_handlers.mcp_proxy import (
        register_mcp_tools, unregister_mcp_server,
    )
    from app.services.tool_registry import registry

    row = _Row("rescan-server", "unused")
    try:
        register_mcp_tools(row, [
            {"name": "kept", "description": "", "inputSchema": {}},
            {"name": "removed", "description": "", "inputSchema": {}},
        ])
        assert registry.get_entry("mcp__rescan_server__removed") is not None

        register_mcp_tools(row, [
            {"name": "kept", "description": "", "inputSchema": {}},
        ])
        assert registry.get_entry("mcp__rescan_server__kept") is not None
        assert registry.get_entry("mcp__rescan_server__removed") is None
    finally:
        unregister_mcp_server(row)


def test_grants_scope_tools_to_named_agents():
    from app.services.tool_handlers.mcp_proxy import (
        register_mcp_tools, unregister_mcp_server, mcp_tools_for_agent,
    )

    row = _Row("granted-server", "unused")
    try:
        register_mcp_tools(row, [{"name": "echo", "description": "", "inputSchema": {}}])
        # Empty grant list = every agent (including sessions with no agent_name).
        names = {s["function"]["name"] for s in mcp_tools_for_agent(None)}
        assert "mcp__granted_server__echo" in names

        from app.services.tool_handlers.mcp_proxy import refresh_grants

        refresh_grants(row, ["analyst"])
        assert mcp_tools_for_agent("analyst"), "granted agent lost its tools"
        assert mcp_tools_for_agent("intruder") == []
        assert mcp_tools_for_agent(None) == []
    finally:
        unregister_mcp_server(row)


def test_get_tools_appends_mcp_tools_but_honours_no_tools_contract():
    from app.services.agent_prompts import get_tools
    from app.services.tool_handlers.mcp_proxy import (
        register_mcp_tools, unregister_mcp_server,
    )

    row = _Row("prompt-server", "unused")
    try:
        register_mcp_tools(row, [
            {"name": "lookup", "description": "L", "inputSchema": {"type": "object"}},
        ])
        names = [t["function"]["name"] for t in get_tools("general_assistant")]
        assert "mcp__prompt_server__lookup" in names

        # workflow_builder's explicit empty tool list is a hard contract.
        assert get_tools("workflow_builder", {"enabled_tools": []}) == []
        # ...and so is an explicit empty list on any other agent.
        assert get_tools("general_assistant", {"enabled_tools": []}) == []
    finally:
        unregister_mcp_server(row)


@pytest.mark.asyncio
async def test_forwarder_calls_the_real_server_through_the_registry():
    """The whole chain: registry entry → row lookup → stdio MCP call."""
    from app.services.mcp_client.session import get_session_manager
    from app.services.tool_handlers.mcp_proxy import (
        register_mcp_tools, unregister_mcp_server,
    )
    from app.services.tool_registry import registry

    row_id = _make_row("live-mcp-server", _server_url())
    try:
        # Register with a row-shaped object carrying the persisted id — the
        # published handler must route by that id, not by request context.
        register_mcp_tools(
            _IdRow(row_id, "live-mcp-server", _server_url()),
            [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}],
        )

        handler = registry.get_handler("mcp__live_mcp_server__echo")
        assert handler is not None
        result = await handler({"hello": "registry"}, None, None, {})
        assert result["success"] is True, result
        assert "registry" in result["result"]
    finally:
        await get_session_manager().close_all()
        unregister_mcp_server(row_id)
        _drop_row(row_id)


@pytest.mark.asyncio
async def test_forwarder_reports_a_removed_server_without_raising():
    from app.services.tool_handlers.mcp_proxy import (
        register_mcp_tools, unregister_mcp_server, mcp_registry_name,
    )
    from app.services.tool_registry import registry
    from app.services.tool_registry import registry as _registry

    row_id = _make_row("doomed-server", "unused")
    register_mcp_tools(_IdRow(row_id, "doomed-server", "unused"),
                       [{"name": "echo", "description": "", "inputSchema": {}}])
    handler = registry.get_handler(mcp_registry_name("doomed-server", "echo"))

    # Row soft-deleted behind the registry's back.
    from app.database import SessionLocal
    from app.models.mcp_server import McpServer

    db = SessionLocal()
    db.query(McpServer).filter(McpServer.id == row_id).update({"is_deleted": True})
    db.commit()
    db.close()

    result = await handler({}, None, None, {})
    assert result["success"] is False
    assert "no longer connected" in result["error"]
    unregister_mcp_server(row_id)
    _drop_row(row_id)
    assert _registry.get_entry(mcp_registry_name("doomed-server", "echo")) is None


def test_forwards_only_the_owning_servers_tool():
    """Two servers exposing the same tool name must not collide."""
    from app.services.tool_handlers.mcp_proxy import (
        register_mcp_tools, unregister_mcp_server,
    )
    from app.services.tool_registry import registry

    a = _IdRow("server-a-id", "server-a", "unused")
    b = _IdRow("server-b-id", "server-b", "unused")
    try:
        register_mcp_tools(a, [{"name": "search", "description": "A", "inputSchema": {}}])
        register_mcp_tools(b, [{"name": "search", "description": "B", "inputSchema": {}}])
        assert registry.get_entry("mcp__server_a__search") is not None
        assert registry.get_entry("mcp__server_b__search") is not None
        assert (registry.get_entry("mcp__server_a__search").schema
                ["function"]["description"] == "[server-a] A")
        assert (registry.get_entry("mcp__server_b__search").schema
                ["function"]["description"] == "[server-b] B")
    finally:
        unregister_mcp_server(a)
        unregister_mcp_server(b)


class _IdRow:
    """Row stand-in with a distinct id and name (registration keys on id)."""

    def __init__(self, row_id: str, name: str, server_url: str, transport: str = "stdio"):
        self.id = row_id
        self.name = name
        self.server_url = server_url
        self.base_url = None
        self.transport = transport
        self.oauth_token_json = None
        self.granted_agents = []
        self.is_deleted = False


# ─── Access control on the MCP endpoints (2026-09-17) ─────────────────────
#
# A stdio MCP server's server_url IS a shell command, and the connect endpoint
# spawned it with no authorization check at all.


def test_viewer_role_is_denied_mcp_servers():
    from app.services.authz.base import ResourceType
    from app.services.authz.rbac import RbacAuthorizationProvider

    authz = RbacAuthorizationProvider()
    assert authz.can_access(
        role="viewer", resource_type=ResourceType.MCP_SERVER, resource_id="any",
    ).allowed is False
    assert authz.can_access(
        role="user", resource_type=ResourceType.MCP_SERVER, resource_id="any",
    ).allowed is True
    assert authz.can_access(
        role="admin", resource_type=ResourceType.MCP_SERVER, resource_id="any",
    ).allowed is True
    # Unknown roles fail closed, as everywhere else in this provider.
    assert authz.can_access(
        role="ghost", resource_type=ResourceType.MCP_SERVER, resource_id="any",
    ).allowed is False


def test_connect_endpoint_rejects_a_viewer():
    from fastapi import HTTPException

    from app.routers.mcp import _require_mcp_access
    from app.services.tracing import TraceContext

    TraceContext.clear()
    TraceContext.set(role="viewer")
    try:
        with pytest.raises(HTTPException) as exc:
            _require_mcp_access("remote-shell")
        assert exc.value.status_code == 403
    finally:
        TraceContext.clear()

    # A normal user passes — the gate must not break the working path.
    TraceContext.set(role="user")
    try:
        _require_mcp_access("remote-shell")
    finally:
        TraceContext.clear()
