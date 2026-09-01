"""MCP proxy JSON-RPC round-trip tests (stdio transport)."""

import json
import sys
import tempfile
import textwrap
from pathlib import Path

from app.services.tool_handlers.mcp_proxy import (
    _split_command,
    call_mcp_tool,
    list_tools_for_server,
)

_SERVER = r'''
import json, sys
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
        if method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "echo", "description": "Echo back the arguments",
                 "inputSchema": {"type": "object", "properties": {}}}
            ]}}
        elif method == "tools/call":
            params = req.get("params") or {}
            args = params.get("arguments") or {}
            if (params.get("name") or "") != "echo":
                resp = {"jsonrpc": "2.0", "id": rid, "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "unknown tool"}]}}
            else:
                resp = {"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": json.dumps(args)}]}}
        else:
            resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown method"}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

main()
'''


def _server_url() -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(textwrap.dedent(_SERVER))
        path = f.name
    return f"{sys.executable} {path}"


def test_split_command_parses_url():
    cmd, args = _split_command("npx -y @scope/feishu --flag")
    assert cmd == "npx"
    assert args == ["-y", "@scope/feishu", "--flag"]


def test_list_tools_for_server_probes_live():
    tools = list_tools_for_server(_server_url())
    assert any(t.get("name") == "echo" for t in tools)


def test_call_mcp_tool_round_trip():
    result = call_mcp_tool(_server_url(), "echo", {"input": "hello mcp"})
    assert result["success"] is True
    assert "hello mcp" in result["result"]


def test_call_mcp_tool_missing_tool_returns_error():
    result = call_mcp_tool(_server_url(), "does_not_exist", {})
    assert result["success"] is False
    assert result["error"]
