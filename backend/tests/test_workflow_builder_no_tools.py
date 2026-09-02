"""Regression: workflow_builder (Build-with-AI) must resolve to ZERO tools.

2026-09-02: the builder has ``tool_config.enabled_tools: []`` — an explicit
"no tools" declaration — but ``_get_tools_base`` treated the empty list as
falsy, fell through to DEFAULT_USER_AGENT_TOOLS (incl. create_artifact), and
the LLM tried to emit the workflow graph as a markdown artifact. The v3
deliverable phase-lock blocked the call, the file-turn guard appended
"(The requested Markdown document could not be generated within this turn's
tool budget)", and the graph was never saved.

These tests pin the contract: an explicit EMPTY enabled_tools list resolves
to no tools at all, while a missing tool_config still falls back to the
default user tool set.
"""
from __future__ import annotations

from app.services.agent_prompts import _get_tools_base, get_tools


def _tool_names(tools) -> list[str]:
    out = []
    for t in tools:
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, dict):
            out.append(t.get("name") or t.get("function", {}).get("name") or "")
        elif getattr(t, "name", None):
            out.append(t.name)
    return out


def test_workflow_builder_explicit_empty_tools_resolves_to_none():
    """enabled_tools=[] means the agent deliberately has NO tools.

    Asserted at the base resolver level: the public get_tools() wrapper
    intentionally appends the deck-edit tools (edit_slide/add_slide) for
    all agents when DECK_EDIT_ROUTING_ENABLED is on — that is platform
    behavior, not the empty-tools contract.
    """
    tools = _get_tools_base("workflow_builder", {"enabled_tools": []})
    assert tools == []


def test_workflow_builder_has_no_create_artifact():
    """The builder must never receive the artifact tool (regression)."""
    tools = get_tools("workflow_builder", {"enabled_tools": []})
    assert "create_artifact" not in _tool_names(tools)


def test_workflow_builder_gets_no_deck_tools_even_with_routing_enabled(monkeypatch):
    """The deck-edit append must NOT violate the builder's no-tools contract.

    2026-09-02: ``get_tools`` appends the deck-edit schemas (edit_slide,
    add_slide, ...) for EVERY agent when DECK_EDIT_ROUTING_ENABLED is on —
    including workflow_builder, whose system prompt says "You have NO tools
    available. Never attempt tool calls." The LLM then saw
    edit_slide/add_slide/restyle_deck/update_chart/remove_slide/reorder_slide
    in its function list and argued it had tools ("my available tools are
    limited to edit_slide, add_slide, ...") instead of emitting the
    save_flow decision summary. The explicit empty enabled_tools list is a
    hard contract: the builder gets ZERO tools, deck-edit included.
    """
    import app.services.agent_prompts as ap

    monkeypatch.setattr(ap.settings, "DECK_EDIT_ROUTING_ENABLED", True)
    tools = get_tools("workflow_builder", {"enabled_tools": []})
    assert _tool_names(tools) == []


def test_deck_tools_still_appended_for_default_agents(monkeypatch):
    """The deck-edit feature itself must keep working for non-builder agents."""
    import app.services.agent_prompts as ap

    monkeypatch.setattr(ap.settings, "DECK_EDIT_ROUTING_ENABLED", True)
    tools = get_tools("general_assistant", None)
    names = _tool_names(tools)
    assert "edit_slide" in names
    assert "add_slide" in names


def test_missing_tool_config_still_falls_back_to_defaults():
    """No tool_config (None) keeps the old behavior: default user tools."""
    tools = get_tools("general_assistant", None)
    assert len(tools) > 0
    names = _tool_names(tools)
    assert any("search" in n for n in names)  # default set is non-trivial


def test_nonempty_enabled_tools_still_resolves():
    """A real non-empty list must still resolve via the registry."""
    tools = get_tools("general_assistant", {"enabled_tools": ["web_search"]})
    assert "web_search" in _tool_names(tools)
