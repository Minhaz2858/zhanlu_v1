"""Tests for composable toolsets."""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.composable_toolsets import (
    Toolset,
    register_toolset,
    get_toolset,
    list_toolsets,
    resolve_tools,
    resolve_from_agent_config,
)


def test_builtin_toolsets_exist():
    """Coding, research, and safe toolsets are registered."""
    assert "coding" in list_toolsets()
    assert "research" in list_toolsets()
    assert "safe" in list_toolsets()


def test_coding_has_write_file():
    """Coding posture includes write_file."""
    ts = get_toolset("coding")
    assert "write_file" in ts.tools


def test_safe_does_not_have_write_file():
    """Safe posture excludes write_file."""
    ts = get_toolset("safe")
    assert "write_file" not in ts.tools


def test_research_has_ask_data_agent():
    """Research posture includes ask_data_agent."""
    ts = get_toolset("research")
    assert "ask_data_agent" in ts.tools


def test_resolve_single_toolset():
    """resolve_tools returns tools from a single toolset."""
    tools = resolve_tools(["safe"])
    assert "read_file" in tools
    assert "write_file" not in tools


def test_resolve_multiple_toolsets():
    """resolve_tools merges tools from multiple toolsets."""
    tools = resolve_tools(["safe", "coding"])
    assert "read_file" in tools
    assert "write_file" in tools  # from coding


def test_resolve_with_extras():
    """resolve_tools includes extra tools."""
    tools = resolve_tools(["safe"], extra_tools=["custom_tool"])
    assert "custom_tool" in tools
    assert "read_file" in tools


def test_resolve_with_exclusions():
    """resolve_tools excludes specified tools."""
    tools = resolve_tools(["coding"], exclude_tools=["write_file"])
    assert "write_file" not in tools
    assert "read_file" in tools


def test_resolve_unknown_toolset():
    """resolve_tools handles unknown toolsets gracefully."""
    tools = resolve_tools(["nonexistent"])
    assert tools == []


def test_resolve_recursive_includes():
    """resolve_tools follows includes recursively."""
    parent = Toolset(
        name="parent",
        tools={"parent_tool"},
        includes=["child"],
    )
    child = Toolset(
        name="child",
        tools={"child_tool"},
        includes=["grandchild"],
    )
    grandchild = Toolset(
        name="grandchild",
        tools={"grandchild_tool"},
    )
    register_toolset(parent)
    register_toolset(child)
    register_toolset(grandchild)

    tools = resolve_tools(["parent"])
    assert "parent_tool" in tools
    assert "child_tool" in tools
    assert "grandchild_tool" in tools


def test_register_custom_toolset():
    """Custom toolsets can be registered."""
    custom = Toolset(name="custom_test", tools={"my_tool"}, description="test")
    register_toolset(custom)
    assert "custom_test" in list_toolsets()
    ts = get_toolset("custom_test")
    assert "my_tool" in ts.tools


def test_resolve_from_agent_config_posture():
    """resolve_from_agent_config uses posture when set."""
    tools = resolve_from_agent_config(None, posture="safe")
    assert "read_file" in tools
    assert "write_file" not in tools


def test_resolve_from_agent_config_enabled_tools():
    """resolve_from_agent_config uses enabled_tools when no posture."""
    config = {"enabled_tools": ["read_file", "web_search"]}
    tools = resolve_from_agent_config(config)
    assert "read_file" in tools
    assert "web_search" in tools


def test_resolve_from_agent_config_default():
    """resolve_from_agent_config falls back to coding posture."""
    tools = resolve_from_agent_config(None)
    assert "read_file" in tools
    assert "write_file" in tools  # coding includes write
