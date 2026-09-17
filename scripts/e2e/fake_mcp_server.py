"""Minimal spec-compliant MCP server for live end-to-end tests.

Implements the real handshake (``initialize`` -> ``notifications/initialized``)
so a client that skips it gets the same ``-32002 Server not initialized`` a
production server would return. Exposes two tools:

* ``echo``     — returns the arguments it was given (proves arg pass-through)
* ``add``      — returns a computed number (proves the result flows back)

Run standalone:  python fake_mcp_server.py
"""

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the JSON arguments you pass.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two integers and return the sum.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
    },
]

_initialized = False


def _reply(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    global _initialized
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
            _initialized = True
            _reply(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "e2e-fake-mcp", "version": "1.0.0"},
            })
            continue
        if method in ("notifications/initialized", "initialized"):
            continue
        if not _initialized:
            # Same refusal a real server gives to a client that skips the handshake.
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32002, "message": "Server not initialized"}}) + "\n")
            sys.stdout.flush()
            continue
        if method == "ping":
            _reply(rid, {})
        elif method == "tools/list":
            _reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                _reply(rid, {"content": [{"type": "text",
                                          "text": f"ECHO_MARKER:{json.dumps(args)}"}]})
            elif name == "add":
                total = int(args.get("a", 0)) + int(args.get("b", 0))
                _reply(rid, {"content": [{"type": "text",
                                          "text": f"SUM_MARKER:{total}"}]})
            else:
                _reply(rid, {"isError": True,
                             "content": [{"type": "text", "text": f"unknown tool {name}"}]})
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": "unknown method"}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
