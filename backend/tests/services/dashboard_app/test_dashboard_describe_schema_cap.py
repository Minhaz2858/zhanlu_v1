"""T12: per-turn describe_schema cap for dashboard turns.

The v3 stream loop must stop letting the agent re-inspect the schema
(``describe_schema``) once ``MAX_DESCRIBE_SCHEMA_PER_DASHBOARD_TURN`` is
reached, otherwise it burns the whole tool-loop budget exploring instead of
calling ``create_fullstack_dashboard``. The pure decision helper lives in
``dashboard_turn_guard.describe_schema_cap_reached`` and the v3 loop feeds it
the canonical executed tool names of the current turn.
"""
import pytest

from app.config import settings
from app.services.dashboard_turn_guard import (
    DASHBOARD_SCHEMA_CAP_TOOLS,
    dashboard_build_tool,
    dashboard_guard_blocked_tools,
    describe_schema_cap_reached,
)


@pytest.fixture
def fullstack_on(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    yield


# ── helper unit tests ───────────────────────────────────────────────────────

def test_cap_fires_after_cap_executions(fullstack_on):
    """After max_cap describe_schema calls, the NEXT one is blocked."""
    executed = ["list_data_sources", "describe_schema", "describe_schema"]
    assert describe_schema_cap_reached("build me a dashboard", executed, 2) is True


def test_cap_not_fired_below_cap(fullstack_on):
    executed = ["describe_schema"]  # only 1 of 2
    assert describe_schema_cap_reached("build me a dashboard", executed, 2) is False


def test_cap_off_when_flag_zero(fullstack_on):
    executed = ["describe_schema"] * 10
    assert describe_schema_cap_reached("build me a dashboard", executed, 0) is False


def test_cap_inert_for_non_dashboard_request(fullstack_on):
    executed = ["describe_schema"] * 5
    assert describe_schema_cap_reached("summarize the weekly report", executed, 2) is False
    assert describe_schema_cap_reached("", executed, 2) is False
    assert describe_schema_cap_reached(None, executed, 2) is False


def test_cap_inert_when_build_already_attempted(fullstack_on):
    """After the build tool ran, schema inspection is legitimate iteration."""
    executed = ["describe_schema", "describe_schema", "create_fullstack_dashboard"]
    assert describe_schema_cap_reached("build me a dashboard", executed, 2) is False


def test_cap_inert_when_flags_off(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    executed = ["describe_schema"] * 5
    assert describe_schema_cap_reached("build me a dashboard", executed, 2) is False


def test_cap_active_with_legacy_tool(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", True)
    executed = ["describe_schema", "describe_schema"]
    assert dashboard_build_tool() == "create_dashboard"
    assert describe_schema_cap_reached("build me a dashboard", executed, 2) is True


def test_cap_counts_only_describe_schema(fullstack_on):
    executed = ["describe_schema", "execute_query", "execute_query"]
    assert describe_schema_cap_reached("build me a dashboard", executed, 2) is False


def test_empty_executed_list(fullstack_on):
    assert describe_schema_cap_reached("build me a dashboard", [], 2) is False
    assert describe_schema_cap_reached("build me a dashboard", None, 2) is False


# ── constants / integration with guard machinery ────────────────────────────

def test_cap_tool_constant():
    assert DASHBOARD_SCHEMA_CAP_TOOLS == frozenset({"describe_schema"})


def test_cap_blocked_set_disjoint_from_guard_blocked_tools(fullstack_on):
    """The cap's blocked set must never overlap the guard's blocked set, and
    must never include the build tool itself."""
    assert not (DASHBOARD_SCHEMA_CAP_TOOLS & dashboard_guard_blocked_tools())
    assert "create_fullstack_dashboard" not in DASHBOARD_SCHEMA_CAP_TOOLS
    assert "create_dashboard" not in DASHBOARD_SCHEMA_CAP_TOOLS


def test_cap_composes_with_guard_blocked_tools(fullstack_on):
    """A dashboard turn that over-inspects schema fires the cap even when the
    classic guard would also block an execute_query."""
    executed = ["describe_schema", "describe_schema", "execute_query"]
    assert "execute_query" in dashboard_guard_blocked_tools()
    assert describe_schema_cap_reached("build me a dashboard", executed, 2) is True
