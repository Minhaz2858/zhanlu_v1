"""Reverse-aliased tool names must dispatch to the canonical underscore tool.

The LLM sometimes hallucinates the dotted display label (e.g. ``skills.hub``)
instead of the registered underscore name (``skills_hub``). The dispatcher
should treat the dotted form as an alias and route to the canonical tool.

These tests pin two things:

1. The contents of ``TOOL_NAME_ALIASES`` (the dotted->canonical map).
2. The end-to-end contract: when ``execute_tool`` is called with a dotted
   name, the registered handler for the canonical underscore name receives
   the call.

If you add a new dotted display name to ``TOOL_DISPLAY_NAMES`` in
``app/routers/agents.py``, you must also:

- Add the matching pair to ``TOOL_NAME_ALIASES`` in
  ``app/services/agent_tools.py`` (else ``execute_tool`` returns
  ``Unknown tool: <dotted>``).
- Add the pair to ``EXPECTED_ALIAS_PAIRS`` below (else the contract test
  silently passes without covering the new alias).
"""
import asyncio
import pytest
from unittest.mock import MagicMock

from app.services.agent_tools import TOOL_NAME_ALIASES, execute_tool
from app.services.tool_registry import registry


# Each pair: (dotted-name-llm-might-hallucinate, canonical-underscore-name).
# Sourced from TOOL_DISPLAY_NAMES in app/routers/agents.py.
EXPECTED_ALIAS_PAIRS = [
    ("skills.hub", "skills_hub"),
    ("skills.sync", "skills_sync"),
    ("skills.guard", "skills_guard"),
    ("skill.provenance", "skill_provenance"),
    ("skill.usage", "skill_usage"),
    ("mcp.oauth", "mcp_oauth"),
    ("mcp.oauth_manager", "mcp_oauth_manager"),
    ("process_registry.list", "process_registry_list"),
    ("process_registry.tail", "process_registry_tail"),
    ("process_registry.kill", "process_registry_kill"),
]


# ---------- Contract tests on the alias map itself ----------


def test_tool_name_aliases_constant_is_non_empty_dict():
    assert isinstance(TOOL_NAME_ALIASES, dict) and len(TOOL_NAME_ALIASES) > 0


def test_all_dotted_names_present_in_aliases():
    missing = [d for d, _ in EXPECTED_ALIAS_PAIRS if d not in TOOL_NAME_ALIASES]
    assert not missing, f"Missing dotted->canonical entries: {missing}"


def test_alias_targets_are_underscore_form():
    for _, canonical in EXPECTED_ALIAS_PAIRS:
        assert "." not in canonical, (
            f"Canonical {canonical!r} must be underscore form, not dotted"
        )


@pytest.mark.parametrize("dotted,canonical", EXPECTED_ALIAS_PAIRS)
def test_dotted_to_canonical_alias(dotted, canonical):
    assert TOOL_NAME_ALIASES[dotted] == canonical


# ---------- End-to-end: dotted name must reach the canonical handler ----------


@pytest.fixture
def mock_canonical_handlers(monkeypatch):
    """Register a MagicMock handler for every canonical name referenced by
    the alias map, so we can call ``execute_tool`` with dotted names and
    assert the mocks receive the calls. Cleaned up after the test.
    """
    for _, canonical in EXPECTED_ALIAS_PAIRS:
        # Use a sync MagicMock; execute_tool's registry path is sync and
        # asyncio.to_thread wraps it.
        handler = MagicMock(return_value={"success": True, "tool": canonical})
        entry = MagicMock()
        entry.handler = handler
        entry.is_async = False
        # Inject into the registry's _tools dict for the duration of the test.
        monkeypatch.setitem(registry._tools, canonical, entry)
    return EXPECTED_ALIAS_PAIRS


def _call_execute_tool(tool_name: str):
    """Run the async execute_tool. The mock handlers don't touch the db,
    so we pass a MagicMock as the db argument to satisfy the signature.
    """
    db = MagicMock()
    return asyncio.run(execute_tool(tool_name, {}, db))


@pytest.mark.parametrize("dotted,canonical", EXPECTED_ALIAS_PAIRS)
def test_dotted_name_dispatches_to_canonical_handler(
    dotted, canonical, mock_canonical_handlers
):
    """The full contract: ``execute_tool('skills.hub', ...)`` ends up calling
    the handler registered as ``skills_hub``.
    """
    result = _call_execute_tool(dotted)
    assert result.get("success") is True
    # The mock handler echoes back the canonical name in its result dict;
    # if the alias did NOT route correctly, execute_tool would return an
    # ``unknown tool`` error before reaching the handler.
    assert result.get("tool") == canonical, (
        f"execute_tool({dotted!r}) did not route to canonical {canonical!r}; "
        f"got result={result!r}"
    )


def test_unknown_dotted_name_returns_unknown_tool_error(monkeypatch):
    """A dotted name that has no entry in TOOL_NAME_ALIASES (and no
    registered handler) must surface a clear 'Unknown tool' error rather
    than raising or hanging.
    """
    monkeypatch.delitem(TOOL_NAME_ALIASES, "skills.hub", raising=False)
    # Make sure 'skills.hub' is also not in the registry, so neither path
    # can resolve it.
    monkeypatch.delitem(registry._tools, "skills.hub", raising=False)
    result = _call_execute_tool("skills.hub")
    assert result.get("success") is False
    # The error message should mention the tool name so the LLM can self-correct.
    assert "skills.hub" in (result.get("error") or "")


def test_alias_resolution_does_not_mutate_unrelated_tool_names():
    """A non-dotted, non-aliased tool_name must pass through unchanged."""
    assert TOOL_NAME_ALIASES.get("not_a_dotted_name", "not_a_dotted_name") == "not_a_dotted_name"
    assert TOOL_NAME_ALIASES.get("create_agent", "create_agent") == "create_agent"
    assert TOOL_NAME_ALIASES.get("", "") == ""
