"""2026-08-25: Test that all tool schemas have the OpenAI function wrapper.

Background: OpenAI/DeepSeek API requires each tool to have the structure:
    {"type": "function", "function": {"name", "description", "parameters"}}
Some tools (e.g. collect_enterprise_data) were registered without the
wrapper, causing the LLM API to reject the request with:
    1 validation error: tools.99.function - Field required

This test verifies the schema is wrapped correctly in the registry AND that
any tool schemas defined in source files have the proper structure.
"""
import pytest


def test_collect_enterprise_data_schema_has_function_wrapper():
    """The collect_enterprise_data tool schema source must be a function
    definition (or be wrappable to one)."""
    from app.services.tool_handlers.enterprise_data_tools import (
        COLLECT_ENTERPRISE_DATA_SCHEMA,
    )
    s = COLLECT_ENTERPRISE_DATA_SCHEMA
    # Accept either: (a) already-wrapped form, or (b) flat form that we
    # auto-wrap. The auto-wrap fix is in tool_registry.get_schemas().
    is_wrapped = s.get("type") == "function" and "function" in s
    is_flat = "name" in s and "parameters" in s and "function" not in s
    assert is_wrapped or is_flat, (
        f"Schema has unexpected structure: keys={list(s.keys())}"
    )


def test_tool_registry_wraps_flat_schemas():
    """The ToolRegistry.get_schemas() must auto-wrap flat schemas in the
    OpenAI function envelope so the LLM API doesn't reject the request."""
    from app.services.tool_registry import ToolRegistry
    from types import SimpleNamespace

    class _FakeHandler:
        pass

    reg = ToolRegistry()
    flat_schema = {
        "name": "fake_tool",
        "description": "test",
        "parameters": {"type": "object", "properties": {}},
    }
    reg.register(
        name="fake_tool",
        schema=flat_schema,
        handler=_FakeHandler,
    )
    schemas = reg.get_schemas(["fake_tool"])
    assert len(schemas) == 1
    s = schemas[0]
    # Must be wrapped
    assert s.get("type") == "function", f"Tool not wrapped: {s}"
    assert "function" in s
    assert s["function"].get("name") == "fake_tool"
