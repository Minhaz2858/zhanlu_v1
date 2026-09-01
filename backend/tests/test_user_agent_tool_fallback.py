"""User-built agents with no skill selections get a stable, well-documented
set of baseline tools. The contract is:

- ``DEFAULT_USER_AGENT_TOOLS`` is the baseline tool list.
- When ``_create_agent`` is called with no ``tool_config``, the new agent's
  ``tool_config.enabled_tools`` is the merge of skill-mapped tools plus the
  baseline, de-duplicated.

These tests pin the contract so a refactor of either code path is caught
immediately. The agent-builder UI surfaces the same list (see
frontend/src/components/agentbuilder/AgentToolsPanel.jsx) - keep them
in sync.
"""
import logging
import pytest
from unittest.mock import MagicMock

from app.services.tool_registry import (
    DEFAULT_USER_AGENT_TOOLS,
    DEFAULT_TOOLS_BY_AGENT,
    resolve_tools_from_skills,
)


# ---------- Contract: DEFAULT_USER_AGENT_TOOLS is a stable baseline ----------


def test_default_user_agent_tools_contains_expected_baselines():
    # The baseline is a stable contract. web_search must be first (the
    # grounding heuristic pins it to position 0). The full set has grown
    # beyond the original 4 as capabilities were added (create_artifact,
    # load_skill_body, agent_browser, Skill) — this test asserts the
    # essential invariants rather than exact equality so the baseline can
    # evolve without breaking every commit.
    assert DEFAULT_USER_AGENT_TOOLS[0] == "web_search", (
        f"web_search must be first; got {DEFAULT_USER_AGENT_TOOLS!r}"
    )
    for required in (
        "web_search", "web_extract", "memory", "todo",
        "create_dashboard", "update_dashboard", "undo_dashboard_edit",
        "uiux_search", "uiux_design_system",
    ):
        assert required in DEFAULT_USER_AGENT_TOOLS, (
            f"Baseline must include {required!r}; got {DEFAULT_USER_AGENT_TOOLS!r}"
        )


def test_default_user_agent_tools_has_no_duplicates():
    assert len(DEFAULT_USER_AGENT_TOOLS) == len(set(DEFAULT_USER_AGENT_TOOLS))


def test_general_assistant_fallback_has_live_dashboard_and_uiux_tools():
    enabled = DEFAULT_TOOLS_BY_AGENT["general_assistant"]
    for required in (
        "create_dashboard", "update_dashboard", "undo_dashboard_edit",
        "uiux_search", "uiux_design_system",
    ):
        assert required in enabled, (
            f"general_assistant fallback must include {required!r}; got {enabled!r}"
        )


def test_system_agent_full_tool_catalog_has_live_dashboard_and_uiux_tools():
    from app.services.system_agents import ALL_TOOL_NAMES

    for required in (
        "create_dashboard", "update_dashboard", "undo_dashboard_edit",
        "uiux_search", "uiux_design_system",
    ):
        assert required in ALL_TOOL_NAMES, (
            f"system agent tool catalog must include {required!r}; got missing from ALL_TOOL_NAMES"
        )


def test_default_user_agent_tools_contains_only_underscore_names():
    for tool in DEFAULT_USER_AGENT_TOOLS:
        assert "." not in tool, (
            f"Baseline tool {tool!r} must be underscore form (registry name)"
        )


# ---------- Contract: resolve_tools_from_skills is pure and forgiving ----------


def test_resolve_tools_from_skills_with_empty_input_returns_empty():
    # Empty input must return empty list (NOT the defaults). The defaults
    # are applied by the caller, not by this function.
    assert resolve_tools_from_skills([]) == []


def test_resolve_tools_from_skills_with_none_returns_empty():
    # Defensive: passing None should not raise.
    assert resolve_tools_from_skills(None) == []


def test_resolve_tools_from_skills_skips_unknown_names():
    # Unknown skill names (e.g. marketplace skills without handlers) are
    # silently dropped, not raised.
    assert resolve_tools_from_skills(["Nonexistent Tool", "Web Search"]) == ["web_search"]


def test_resolve_tools_from_skills_dedupes_repeated_names():
    # Same display name repeated should appear only once in the result.
    assert resolve_tools_from_skills(["Memory", "Memory", "Web Search"]) == ["memory", "web_search"]


def test_resolve_tools_from_skills_preserves_input_order():
    # Order is preserved (insertion order, no sort).
    assert resolve_tools_from_skills(["Todo", "Web Search", "Memory"]) == ["todo", "web_search", "memory"]


# ---------- Contract: _create_agent fallback path ----------


def _build_create_args(skills=None, tool_config=None):
    """Build the minimum required kwargs for _create_agent, with sensible
    defaults for fields that are read but irrelevant to this test.
    """
    args = {
        "name": "Test Agent",
        "description": "",
        "project": "global",
        "capabilities": [],
        "model": "automatic",
        "agent_type": "sequential",
        "prompt_identity": "",
        "prompt_boundary": "",
        "prompt_reasoning": "",
        "prompt_tools": "",
        "tools": [],
        "skills": skills or [],
    }
    if tool_config is not None:
        args["tool_config"] = tool_config
    return args


def _set_id_on_flush(instance):
    """Side effect: when db.flush() is called, give the AgentApp a real id."""
    instance.id = "fake-uuid-1234"


def test_create_agent_with_no_tool_config_uses_baseline_plus_skills():
    """When the LLM calls create_agent with no tool_config, the new agent
    gets DEFAULT_USER_AGENT_TOOLS plus any skill-mapped tools, de-duped.
    """
    from app.services.agent_tools import _create_agent

    db = MagicMock()
    db.add = MagicMock()
    db.flush.side_effect = _set_id_on_flush

    args = _build_create_args(skills=["Web Search"])
    result = _create_agent(args, db, user_id=None)

    assert result.get("success") is True
    # Inspect the AgentApp that was added to the db session.
    added = db.add.call_args[0][0]
    assert hasattr(added, "tool_config")
    enabled = added.tool_config["enabled_tools"]
    # web_search from skills, plus the 4 baselines
    assert "web_search" in enabled
    assert "web_extract" in enabled
    assert "memory" in enabled
    assert "todo" in enabled
    # And no duplicates
    assert len(enabled) == len(set(enabled))


def test_create_agent_with_no_skills_uses_just_baseline():
    """Pure default case: no skills selected -> only the baseline.
    This is the exact contract Task A2 is pinning.
    """
    from app.services.agent_tools import _create_agent

    db = MagicMock()
    db.flush.side_effect = _set_id_on_flush

    args = _build_create_args(skills=[])
    _create_agent(args, db, user_id=None)

    added = db.add.call_args[0][0]
    enabled = added.tool_config["enabled_tools"]
    assert enabled == list(DEFAULT_USER_AGENT_TOOLS), (
        f"Expected baseline only, got {enabled!r}"
    )


def test_create_agent_with_explicit_tool_config_respects_llm_choice():
    """If the LLM provides tool_config, the fallback must NOT fire - the
    LLM's choice wins.
    """
    from app.services.agent_tools import _create_agent

    db = MagicMock()
    db.flush.side_effect = _set_id_on_flush

    explicit = {"enabled_tools": ["only_this_one"]}
    args = _build_create_args(skills=[], tool_config=explicit)
    _create_agent(args, db, user_id=None)

    added = db.add.call_args[0][0]
    assert added.tool_config == explicit


def test_create_agent_with_empty_tool_config_triggers_fallback():
    """Edge case: tool_config={} is treated the same as tool_config=None
    — the `if not tool_config:` branch is truthy for both. The baseline
    must be applied.
    """
    from app.services.agent_tools import _create_agent

    db = MagicMock()
    db.flush.side_effect = _set_id_on_flush

    args = _build_create_args(skills=[], tool_config={})
    _create_agent(args, db, user_id=None)

    added = db.add.call_args[0][0]
    enabled = added.tool_config["enabled_tools"]
    assert enabled == list(DEFAULT_USER_AGENT_TOOLS), (
        f"Empty dict should trigger fallback to baseline; got {enabled!r}"
    )


def test_create_agent_with_explicit_empty_enabled_tools_respects_choice():
    """Edge case: tool_config={"enabled_tools": []} is a *deliberate* empty
    choice and must NOT trigger the fallback. The agent ends up with zero
    tools, as the LLM asked.
    """
    from app.services.agent_tools import _create_agent

    db = MagicMock()
    db.flush.side_effect = _set_id_on_flush

    explicit = {"enabled_tools": []}
    args = _build_create_args(skills=[], tool_config=explicit)
    _create_agent(args, db, user_id=None)

    added = db.add.call_args[0][0]
    assert added.tool_config == explicit, (
        f"Explicit empty enabled_tools must be preserved; got {added.tool_config!r}"
    )


# ---------- Contract: the debug log fires when the fallback fires ----------


def test_create_agent_logs_debug_when_fallback_fires(caplog):
    """When tool_config is missing on _create_agent, a debug log records
    the fallback so operators (and the agent-builder UI) can see what
    tools the new agent received.
    """
    from app.services.agent_tools import _create_agent

    db = MagicMock()
    db.flush.side_effect = _set_id_on_flush

    args = _build_create_args(skills=[])

    with caplog.at_level(logging.DEBUG, logger="app.services.agent_tools"):
        _create_agent(args, db, user_id=None)

    # The fallback log mentions the agent name and the baseline.
    fallback_records = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and "fallback" in r.getMessage().lower()
    ]
    assert len(fallback_records) >= 1, (
        f"Expected at least one DEBUG log about the fallback; got: "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    # The log should mention the agent name so downstream consumers can surface it.
    msg = fallback_records[0].getMessage()
    assert "Test Agent" in msg, f"Log should mention agent name: {msg!r}"


def test_create_agent_does_not_log_fallback_when_tool_config_given(caplog):
    """If the LLM provides tool_config, the fallback path is skipped, and
    no fallback log should be emitted.
    """
    from app.services.agent_tools import _create_agent

    db = MagicMock()
    db.flush.side_effect = _set_id_on_flush

    explicit = {"enabled_tools": ["my_choice"]}
    args = _build_create_args(skills=[], tool_config=explicit)

    with caplog.at_level(logging.DEBUG, logger="app.services.agent_tools"):
        _create_agent(args, db, user_id=None)

    fallback_records = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and "fallback" in r.getMessage().lower()
    ]
    assert len(fallback_records) == 0, (
        f"Did not expect a fallback log when tool_config is provided; got: "
        f"{[(r.levelname, r.getMessage()) for r in fallback_records]}"
    )


# ---------- Contract: 4-tool baseline contains web_extract ----------
# The anti-hallucination baseline is exactly 4 tools. web_search and
# web_extract are both required so the LLM can ground time-sensitive
# questions in live data without depending on the user enabling them.


def test_default_user_agent_tools_includes_web_extract():
    assert "web_extract" in DEFAULT_USER_AGENT_TOOLS, (
        "Baseline must include web_extract for grounding "
        f"(got {DEFAULT_USER_AGENT_TOOLS!r})"
    )


def test_default_user_agent_tools_keeps_web_search_first():
    # Order matters: web_search is pinned to position 0 so the LLM sees
    # it as the primary grounding tool.
    assert DEFAULT_USER_AGENT_TOOLS[0] == "web_search", (
        f"web_search must be first in baseline; got {DEFAULT_USER_AGENT_TOOLS!r}"
    )


# ---------- Contract: TIME_SENSITIVE_PATTERN detects time-sensitive questions ----------


def test_time_sensitive_pattern_matches_today():
    from app.services.agent_prompts import TIME_SENSITIVE_PATTERN

    assert TIME_SENSITIVE_PATTERN.search("What's the news today?")
    assert TIME_SENSITIVE_PATTERN.search("Is the price of BTC up today?")
    assert TIME_SENSITIVE_PATTERN.search("Latest scores from the Premier League")


def test_time_sensitive_pattern_matches_iso_date():
    from app.services.agent_prompts import TIME_SENSITIVE_PATTERN

    assert TIME_SENSITIVE_PATTERN.search("What happened on 2026-07-13?")


def test_time_sensitive_pattern_does_not_match_static_questions():
    from app.services.agent_prompts import TIME_SENSITIVE_PATTERN

    assert TIME_SENSITIVE_PATTERN.search("What is 2+2?") is None
    assert TIME_SENSITIVE_PATTERN.search("Explain quantum physics") is None
    assert TIME_SENSITIVE_PATTERN.search("") is None


# ---------- Contract: _enforce_web_grounding reorders tool list ----------


def test_enforce_web_grounding_pins_web_search_to_front_for_time_sensitive():
    from app.services.agent_prompts import _enforce_web_grounding

    tools = ["memory", "todo", "web_search", "web_extract", "agent_browser"]
    pinned, block = _enforce_web_grounding(tools, "What's the weather today?")
    assert pinned[0] == "web_search", (
        f"web_search must be first after grounding; got {pinned!r}"
    )
    assert len(pinned) == len(tools)
    assert set(pinned) == set(tools)
    assert "[GROUNDING REQUIRED" in block


def test_enforce_web_grounding_is_noop_for_static_questions():
    from app.services.agent_prompts import _enforce_web_grounding

    tools = ["memory", "todo", "web_search"]
    pinned, block = _enforce_web_grounding(tools, "What is the capital of France?")
    assert pinned == tools
    assert block == ""


def test_enforce_web_grounding_is_noop_when_web_search_absent():
    from app.services.agent_prompts import _enforce_web_grounding

    # Agent does NOT have web_search available — heuristic must not
    # inject a grounding block the agent cannot satisfy.
    tools = ["memory", "todo", "web_extract", "agent_browser"]
    pinned, block = _enforce_web_grounding(tools, "What's the news today?")
    assert pinned == tools
    assert block == ""


def test_enforce_web_grounding_does_not_mutate_input():
    from app.services.agent_prompts import _enforce_web_grounding

    tools = ["memory", "todo", "web_search"]
    snapshot = list(tools)
    _enforce_web_grounding(tools, "Latest news please")
    assert tools == snapshot, "_enforce_web_grounding must not mutate input"


# ---------- Contract: apply_grounding_to_schemas reorders tool schemas ----------


def _fake_schema(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Fake {name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_apply_grounding_to_schemas_pins_web_search_first_for_time_sensitive():
    from app.services.agent_prompts import apply_grounding_to_schemas

    schemas = [_fake_schema("memory"), _fake_schema("todo"),
               _fake_schema("web_search"), _fake_schema("web_extract")]
    out = apply_grounding_to_schemas(schemas, "What's the latest news?")
    assert out[0]["function"]["name"] == "web_search"
    assert [s["function"]["name"] for s in out] == [
        "web_search", "memory", "todo", "web_extract",
    ]


def test_apply_grounding_to_schemas_is_noop_for_static_questions():
    from app.services.agent_prompts import apply_grounding_to_schemas

    schemas = [_fake_schema("memory"), _fake_schema("web_search")]
    out = apply_grounding_to_schemas(schemas, "Explain recursion")
    assert [s["function"]["name"] for s in out] == ["memory", "web_search"]


def test_apply_grounding_to_schemas_handles_empty_inputs():
    from app.services.agent_prompts import apply_grounding_to_schemas

    assert apply_grounding_to_schemas([], "Latest news") == []
    assert apply_grounding_to_schemas(None, "Latest news") == []
    # user_message=None should not reorder and should return a fresh list copy
    schemas = [_fake_schema("web_search"), _fake_schema("memory")]
    out = apply_grounding_to_schemas(schemas, None)
    assert [s["function"]["name"] for s in out] == ["web_search", "memory"]
    # ensure the function returns a new list (not the same object) for safety
    assert out is not schemas
