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
