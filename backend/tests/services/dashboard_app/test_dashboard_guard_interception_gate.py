"""Regression tests for the dashboard-guard interception gate.

Bug observed 2026-08-29 (conv 8ffb436e, skill_agent "Weekly sales report"):

The v2/v3 loop interception previously fired whenever the build tool was
merely REGISTERED in the agent's toolset (``_dash_build_tool in _tool_names``)
AND the batch contained a blocked tool (execute_query/execute_sql/sql_query).
The skill_agent lists ``create_fullstack_dashboard`` in its enabled_tools, so
EVERY ``execute_query`` call in its chat was intercepted with "only
create_dashboard allowed" — even for plain non-dashboard requests like
"weekly sales report". The agent could never query data and fell back to
"I gathered some information but had trouble putting it all together."

The interception must fire ONLY when a dashboard build is genuinely in play:
- the LLM itself called the build tool in this batch, OR
- the dashboard guard forced the build tool this turn (``dashboard_forced``).
"""
import pytest

from app.config import settings
from app.services.dashboard_turn_guard import (
    dashboard_guard_blocked_tools,
    dashboard_guard_should_block_queries,
)


@pytest.fixture
def fullstack_on(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    yield


BUILD = "create_fullstack_dashboard"


# ── Core regression: registry presence must NOT trigger the block ──────────

def test_no_block_when_build_tool_merely_registered(fullstack_on):
    """The bug: agent has create_fullstack_dashboard in its toolset (registered)
    but the user asked a plain data question. execute_query must NOT be blocked.
    dashboard_forced=False and the build tool is not in this batch → False."""
    assert dashboard_guard_should_block_queries(
        {"execute_query"}, BUILD, dashboard_forced=False,
    ) is False


def test_no_block_when_batch_has_no_blocked_tools(fullstack_on):
    assert dashboard_guard_should_block_queries(
        {"web_search"}, BUILD, dashboard_forced=True,
    ) is False


def test_no_block_without_build_tool(fullstack_on):
    """Guard inert when no build tool is flag-enabled (dashboard_build_tool()
    returns None)."""
    assert dashboard_guard_should_block_queries(
        {"execute_query"}, None, dashboard_forced=True,
    ) is False


def test_no_block_with_empty_batch(fullstack_on):
    assert dashboard_guard_should_block_queries(
        [], BUILD, dashboard_forced=True,
    ) is False
    assert dashboard_guard_should_block_queries(
        None, BUILD, dashboard_forced=True,
    ) is False


# ── The legit trigger paths ─────────────────────────────────────────────────

def test_block_after_force_fired(fullstack_on):
    """Dashboard guard forced the build (dashboard_forced=True), model still
    returns execute_query → block it and redirect to the build tool."""
    assert dashboard_guard_should_block_queries(
        {"execute_query"}, BUILD, dashboard_forced=True,
    ) is True


def test_block_when_llm_calls_build_tool_in_same_batch(fullstack_on):
    """Model emits create_fullstack_dashboard + execute_query in ONE batch —
    the query sibling is waste, block it even without a prior force."""
    assert dashboard_guard_should_block_queries(
        {"create_fullstack_dashboard", "execute_query"}, BUILD, dashboard_forced=False,
    ) is True


@pytest.mark.parametrize(
    "blocked_name",
    ["execute_query", "execute_sql", "sql_query"],
)
def test_block_all_blocked_tool_names(fullstack_on, blocked_name):
    assert dashboard_guard_should_block_queries(
        {blocked_name}, BUILD, dashboard_forced=True,
    ) is True


def test_block_set_matches_guard_constant(fullstack_on):
    assert dashboard_guard_blocked_tools() == frozenset(
        {"execute_query", "execute_sql", "sql_query"}
    )


def test_build_tool_in_batch_alone_does_not_block(fullstack_on):
    """Model calls ONLY the build tool — no blocked sibling → nothing to block."""
    assert dashboard_guard_should_block_queries(
        {BUILD}, BUILD, dashboard_forced=False,
    ) is False


# ── Sibling allowed tools pass through ─────────────────────────────────────

def test_harmless_tools_not_blocked_when_forced(fullstack_on):
    """After a force, harmless tools (web_search etc.) are NOT in the blocked
    set and must not be intercepted."""
    assert dashboard_guard_should_block_queries(
        {"web_search"}, BUILD, dashboard_forced=True,
    ) is False


def test_legacy_build_tool_name(fullstack_on):
    """Legacy mode: build tool is create_dashboard; same gate applies."""
    assert dashboard_guard_should_block_queries(
        {"execute_query"}, "create_dashboard", dashboard_forced=True,
    ) is True
    assert dashboard_guard_should_block_queries(
        {"execute_query"}, "create_dashboard", dashboard_forced=False,
    ) is False
