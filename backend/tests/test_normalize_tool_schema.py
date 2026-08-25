"""2026-08-25: Test the centralized tool-format normalizer.

Background: Multiple sites in the codebase add tools to the LLM call's
tool list, and at least one of them (data_source_runtime) bypasses the
tool_registry.get_schemas() wrapping. So tools[N] in flat form reach
the LLM and DeepSeek rejects with:
  tools[N]: missing field `type` (status 400)
This test verifies the centralized normalize_tool_schema() helper
correctly wraps any tool in the OpenAI function envelope.
"""
import pytest
from app.services.tool_registry import normalize_tool_schema, normalize_tools_list


def test_already_wrapped_schema_is_unchanged():
    """A properly-wrapped schema should be returned unchanged (idempotent)."""
    wrapped = {
        "type": "function",
        "function": {
            "name": "ask_data_agent",
            "description": "ask",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    result = normalize_tool_schema(wrapped)
    assert result is wrapped or result == wrapped  # idempotent


def test_flat_schema_is_wrapped():
    """A flat schema ({name, description, parameters}) should be wrapped."""
    flat = {
        "name": "collect_enterprise_data",
        "description": "collect",
        "parameters": {"type": "object", "properties": {}},
    }
    result = normalize_tool_schema(flat)
    assert result.get("type") == "function"
    assert "function" in result
    assert result["function"]["name"] == "collect_enterprise_data"


def test_fallback_name_used_when_name_missing():
    """If the schema has no 'name', fallback_name should be used."""
    no_name = {
        "description": "test",
        "parameters": {"type": "object", "properties": {}},
    }
    result = normalize_tool_schema(no_name, fallback_name="my_tool")
    assert result["function"]["name"] == "my_tool"


def test_normalize_tools_list_wraps_each_tool():
    """The list-level normalizer should wrap each tool independently."""
    tools = [
        {"name": "tool_a", "description": "a", "parameters": {}},
        {"type": "function", "function": {"name": "tool_b", "description": "b", "parameters": {}}},
        {"name": "tool_c", "description": "c", "parameters": {}},
    ]
    result = normalize_tools_list(tools)
    assert len(result) == 3
    assert result[0]["type"] == "function"
    assert result[0]["function"]["name"] == "tool_a"
    assert result[1]["type"] == "function"
    assert result[1]["function"]["name"] == "tool_b"
    assert result[2]["type"] == "function"
    assert result[2]["function"]["name"] == "tool_c"


def test_normalize_tools_list_handles_empty():
    """Empty/None should be passed through."""
    assert normalize_tools_list(None) is None
    assert normalize_tools_list([]) == []


def test_normalize_tools_list_realistic_ecisco_bi_scenario():
    """Simulate the Ecisco BI scenario: 100+ tools, some flat, some wrapped."""
    tools = []
    # 95 properly wrapped
    for i in range(95):
        tools.append({
            "type": "function",
            "function": {"name": f"tool_{i}", "description": "d", "parameters": {}},
        })
    # 5 flat (the ones the user was hitting)
    for i in range(5):
        tools.append({"name": f"flat_tool_{i}", "description": "f", "parameters": {}})
    result = normalize_tools_list(tools)
    # Every tool must have the wrapped form
    for i, t in enumerate(result):
        assert t.get("type") == "function", f"Tool #{i} not wrapped: {t}"
        assert "function" in t
