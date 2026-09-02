"""Tests for the 2026-spec MCP client (Phase 1: MCP upgrade).

Covers real protocol interop over stdio with a spawned FastMCP server,
the HTTP transport against a local streamable-HTTP app, OAuth token
exchange (mocked HTTP), and the marketplace catalog.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import asyncio
import json
import sys

import pytest

STDIO_SERVER_SCRIPT = r"""
import asyncio
from mcp.server.fastmcp import FastMCP

m = FastMCP("test-stdio-server", instructions="Tiny echo server for client tests")

@m.tool()
async def echo(text: str) -> str:
    return f"echo:{text}"

@m.tool()
async def add(a: float, b: float) -> float:
    return a + b

asyncio.run(m.run_stdio_async())
"""


def test_marketplace_catalog():
    from app.services.mcp_client.registry import get_marketplace

    catalog = get_marketplace()
    assert isinstance(catalog, list) and len(catalog) >= 3
    assert all({"id", "name", "description", "transport"} <= set(entry) for entry in catalog)


@pytest.mark.asyncio
async def test_stdio_client_roundtrip():
    from app.services.mcp_client import connect_stdio

    conn = await connect_stdio(
        server_name="test-stdio",
        command=sys.executable,
        args=["-c", STDIO_SERVER_SCRIPT],
        env={},
    )
    try:
        tools = await conn.list_tools()
        names = {t.name for t in tools}
        assert "echo" in names and "add" in names

        result = await conn.call_tool("echo", {"text": "hello"})
        assert result["success"] is True
        assert "echo:hello" in result["result"]

        result = await conn.call_tool("add", {"a": 2, "b": 3})
        assert result["success"] is True
        assert "5" in result["result"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_http_client_roundtrip():
    """Connect over streamable HTTP to a FastMCP ASGI app on a random port."""
    import threading

    import uvicorn
    from mcp.server.fastmcp import FastMCP

    from app.services.mcp_client import connect_http

    mcp_server = FastMCP("test-http-server")

    @mcp_server.tool()
    async def greet(name: str) -> str:
        return f"hi {name}"

    config = uvicorn.Config(
        mcp_server.streamable_http_app,
        host="127.0.0.1",
        port=0,
        log_level="error",
    )
    server = uvicorn.Server(config)
    sock = config.bind_socket()
    port = sock.getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        # Wait for the server to finish startup.
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)

        conn = await connect_http(
            server_name="test-http",
            base_url=f"http://127.0.0.1:{port}/mcp",
        )
        try:
            tools = await conn.list_tools()
            assert {t.name for t in tools} == {"greet"}
            result = await conn.call_tool("greet", {"name": "zhanlu"})
            assert result["success"] is True
            assert "hi zhanlu" in result["result"]
        finally:
            await conn.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_oauth_exchange_and_refresh(monkeypatch):
    """OAuth authorization-code exchange + refresh with mocked HTTP."""
    import httpx

    from app.services.mcp_client import oauth as oauth_mod

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.responses = kwargs.pop("_responses", [])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            data = kwargs.get("data") or {}
            if data.get("grant_type") == "refresh_token":
                return self.responses[1]
            return self.responses[0]

    _req = httpx.Request("POST", "http://idp.test/token")
    token_resp = httpx.Response(200, request=_req, json={
        "access_token": "at-1", "refresh_token": "rt-1",
        "token_type": "Bearer", "expires_in": 3600,
    })
    refresh_resp = httpx.Response(200, request=_req, json={
        "access_token": "at-2", "refresh_token": "rt-2",
        "token_type": "Bearer", "expires_in": 3600,
    })
    monkeypatch.setattr(
        oauth_mod.httpx, "AsyncClient",
        lambda *a, **k: FakeAsyncClient(*a, **k, _responses=[token_resp, refresh_resp]),
    )

    metadata = {
        "authorization_endpoint": "https://idp.test/authorize",
        "token_endpoint": "https://idp.test/token",
    }
    tokens = await oauth_mod.exchange_code(
        metadata=metadata,
        client_id="cid",
        client_secret="secret",
        code="code-1",
        redirect_uri="https://zhanlu.test/callback",
        code_verifier="verifier",
    )
    assert tokens["access_token"] == "at-1"

    refreshed = await oauth_mod.refresh_tokens(
        metadata=metadata,
        client_id="cid",
        client_secret="secret",
        refresh_token="rt-1",
    )
    assert refreshed["access_token"] == "at-2"


def test_oauth_discovery_urls():
    from app.services.mcp_client.oauth import oauth_metadata_url

    assert oauth_metadata_url("https://example.com/mcp/").endswith(
        "/.well-known/oauth-authorization-server"
    )
    assert oauth_metadata_url("https://example.com/mcp").endswith(
        "/mcp/.well-known/oauth-authorization-server"
    )


@pytest.mark.asyncio
async def test_zhanlu_fastmcp_server_over_http():
    """Zhanlu's own FastMCP server is reachable via the 2026 HTTP transport."""
    import threading

    import uvicorn
    from mcp.server.fastmcp import FastMCP

    import app.services.tool_handlers  # noqa: F401 — populate the ToolRegistry
    from app.services.mcp_client import connect_http
    from app.services.mcp_server.fastmcp_app import build_fastmcp_server

    mcp_server: FastMCP = build_fastmcp_server()
    config = uvicorn.Config(
        mcp_server.streamable_http_app,
        host="127.0.0.1",
        port=0,
        log_level="error",
    )
    server = uvicorn.Server(config)
    sock = config.bind_socket()
    port = sock.getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)

        conn = await connect_http("zhanlu-self", f"http://127.0.0.1:{port}/mcp")
        try:
            tools = await conn.list_tools()
            assert len(tools) > 20, "registry tools should be exposed"
            names = {t.name for t in tools}
            assert "web_search" in names or "execute_code" in names or "create_agent" in names
        finally:
            await conn.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
