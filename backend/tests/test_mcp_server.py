"""Tests for the native MCP server and DB-driven skill-to-tool mapping.

Tests the ZhanluMCPServer wrapper, the MCP proxy handler, and the
refactored ``get_skill_to_tool_map(db)`` function.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))


# ─── Native MCP server tests ──────────────────────────────────────────────

class TestZhanluMCPServer:
    """Test the native MCP server wrapper exposing ToolRegistry tools."""

    @pytest.fixture(autouse=True)
    def _ensure_handlers_imported(self):
        """Import tool_handlers so registry is populated."""
        import app.services.tool_handlers  # noqa: F401
        # Also import multimodal handlers
        from app.services.tool_handlers.multimodal import (  # noqa: F401
            image_gen, video_gen, threed_gen, effects,
        )

    def test_server_singleton(self):
        from app.services.mcp_server import get_zhanlu_mcp_server, ZhanluMCPServer
        s1 = get_zhanlu_mcp_server()
        s2 = get_zhanlu_mcp_server()
        assert s1 is s2
        assert isinstance(s1, ZhanluMCPServer)

    def test_list_tools_returns_entries(self):
        from app.services.mcp_server import get_zhanlu_mcp_server
        server = get_zhanlu_mcp_server()
        tools = server.list_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0
        # Each tool should have name, description, inputSchema
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert isinstance(tool["inputSchema"], dict)
            assert tool["inputSchema"].get("type") == "object"

    def test_list_tools_includes_core_tools(self):
        from app.services.mcp_server import get_zhanlu_mcp_server
        server = get_zhanlu_mcp_server()
        tool_names = [t["name"] for t in server.list_tools()]
        for expected in ["web_search", "memory", "todo", "execute_code"]:
            assert expected in tool_names, f"{expected} missing from MCP tools"

    def test_list_tools_includes_multimodal(self):
        from app.services.mcp_server import get_zhanlu_mcp_server
        server = get_zhanlu_mcp_server()
        tool_names = [t["name"] for t in server.list_tools()]
        for expected in ["mm_image_gen", "mm_video_gen"]:
            assert expected in tool_names, f"{expected} missing from MCP tools"

    @pytest.mark.asyncio
    async def test_call_tool_returns_content(self):
        from app.services.mcp_server import get_zhanlu_mcp_server
        server = get_zhanlu_mcp_server()
        content = await server.call_tool("todo", {"action": "list"})
        assert isinstance(content, list)
        assert len(content) > 0
        assert content[0]["type"] == "text"

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self):
        from app.services.mcp_server import get_zhanlu_mcp_server
        server = get_zhanlu_mcp_server()
        content = await server.call_tool("nonexistent_tool_xyz", {})
        assert isinstance(content, list)
        assert "not found" in content[0]["text"].lower() or "not found" in content[0]["text"]

    def test_list_resources(self):
        from app.services.mcp_server import get_zhanlu_mcp_server
        server = get_zhanlu_mcp_server()
        resources = server.list_resources()
        assert isinstance(resources, list)
        assert len(resources) > 0
        for res in resources:
            assert res["uri"].startswith("skill://")
            assert "name" in res
            assert "mimeType" in res

    def test_read_resource_known(self):
        from app.services.mcp_server import get_zhanlu_mcp_server
        server = get_zhanlu_mcp_server()
        tools = server.list_tools()
        if tools:
            first_name = tools[0]["name"]
            content = server.read_resource(f"skill://{first_name}")
            assert len(content) > 0
            assert content[0]["type"] == "text"
            # Should have name + description + schema
            import json
            data = json.loads(content[0]["text"])
            assert "name" in data

    def test_read_resource_unknown(self):
        from app.services.mcp_server import get_zhanlu_mcp_server
        server = get_zhanlu_mcp_server()
        content = server.read_resource("skill://no_such_tool_xyz")
        assert len(content) > 0
        assert "not found" in content[0]["text"].lower() or "not found" in content[0]["text"]

    def test_read_resource_bad_uri(self):
        from app.services.mcp_server import get_zhanlu_mcp_server
        server = get_zhanlu_mcp_server()
        content = server.read_resource("http://example.com")
        assert "Unknown resource" in content[0]["text"]


# ─── DB-driven skill-to-tool mapping tests ──────────────────────────────

class TestSkillToToolMap:
    """Test the refactored DB-driven get_skill_to_tool_map()."""

    def test_static_fallback_works(self):
        """Without a DB session, the static fallback dict is used."""
        from app.services.tool_registry import get_skill_to_tool_map
        mapping = get_skill_to_tool_map(db=None)
        assert mapping["Web Search"] == "web_search"
        assert mapping["Memory"] == "memory"
        assert mapping["Todo"] == "todo"
        assert mapping["Image Gen"] == "mm_image_gen"

    def test_resolve_tools_from_skills_no_db(self):
        """resolve_tools_from_skills works without db (static mapping)."""
        from app.services.tool_registry import resolve_tools_from_skills
        result = resolve_tools_from_skills(["Web Search", "Memory"])
        assert result == ["web_search", "memory"]

    def test_resolve_tools_from_skills_unknown_skipped(self):
        """Unknown skill names are silently skipped."""
        from app.services.tool_registry import resolve_tools_from_skills
        result = resolve_tools_from_skills(["Nonexistent Tool", "Web Search"])
        assert result == ["web_search"]

    def test_resolve_tools_from_skills_deduplicates(self):
        """Duplicate mappings are deduplicated."""
        from app.services.tool_registry import resolve_tools_from_skills
        result = resolve_tools_from_skills(["Web Search", "Web Search", "Memory"])
        assert result == ["web_search", "memory"]

    def test_empty_skills_returns_empty(self):
        from app.services.tool_registry import resolve_tools_from_skills
        assert resolve_tools_from_skills([]) == []
        assert resolve_tools_from_skills(None) == []


# ─── MCP proxy handler tests ──────────────────────────────────────────────

class TestMcpProxy:
    """Test the MCP proxy handler for forwarding calls."""

    @pytest.mark.asyncio
    async def test_proxy_no_tool_name_returns_error(self):
        from app.services.tool_handlers.mcp_proxy import _mcp_proxy_handler
        result = await _mcp_proxy_handler({}, context={})
        assert result["success"] is False
        assert "no tool_name" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_proxy_no_server_name_returns_error(self):
        from app.services.tool_handlers.mcp_proxy import _mcp_proxy_handler
        result = await _mcp_proxy_handler(
            {}, context={"tool_name": "some_tool"}
        )
        assert result["success"] is False
        assert "no mcp_server" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_proxy_not_implemented_returns_placeholder(self):
        from app.services.tool_handlers.mcp_proxy import _mcp_proxy_handler
        result = await _mcp_proxy_handler(
            {},
            context={"tool_name": "github_search", "mcp_server": "github"},
        )
        assert result["success"] is False
        assert "not yet implemented" in result["error"].lower()

    def test_probe_mcp_server_graceful_failure(self):
        """Probe of nonexistent command returns None gracefully."""
        from app.services.tool_handlers.mcp_proxy import probe_mcp_server
        result = probe_mcp_server(
            "nonexistent_command_xyz_123", [], {}
        )
        assert result is None

    def test_register_and_get_mcp_tools(self):
        from app.services.tool_handlers.mcp_proxy import (
            register_mcp_tools, get_mcp_tools_for_server,
        )
        register_mcp_tools("test-server", [
            {"name": "tool_a", "description": "A tool"},
            {"name": "tool_b", "description": "B tool"},
        ])
        tools = get_mcp_tools_for_server("test-server")
        assert len(tools) == 2

        # Unknown server returns empty
        assert get_mcp_tools_for_server("no-server") == []
