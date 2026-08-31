"""Fixes 2-4: defense-in-depth guards for dashboard turns.

Fix 2 — ``dashboard_antitools_should_block``: on a dashboard-shaped turn,
before the build tool has run, ``create_artifact`` and the legacy
``create_dashboard`` are waste that bypasses the full-stack pipeline. The v3
loop blocks them and nudges the model to call ``create_fullstack_dashboard``.

Fix 3 — ``dashboard_exploration_cap_reached``: counts describe_schema +
execute_query + execute_sql + sql_query COMBINED, so a weak model cannot burn
the whole budget on query exploration either (the T12 describe_schema cap only
covers schema inspection).

Fix 4 — ``parse_artifact_title``: duplicate ``create_artifact`` calls with the
same title within one turn are always waste; the parser feeds the per-turn
dedup set.

All pure helpers live in ``dashboard_turn_guard``; the v3 stream loop feeds
them canonical executed tool names of the current turn.
"""
import pytest

from app.config import settings
from app.services.dashboard_turn_guard import (
    DASHBOARD_ANTITOOLS,
    DASHBOARD_EXPLORATION_TOOLS,
    dashboard_antitools_should_block,
    dashboard_build_tool,
    dashboard_exploration_cap_reached,
    dashboard_guard_blocked_tools,
    parse_artifact_title,
)


@pytest.fixture
def fullstack_on(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    yield


# ── Fix 2: anti-tools block ─────────────────────────────────────────────────

def test_antitools_block_fires_pre_build(fullstack_on):
    """Dashboard turn, nothing executed yet → create_artifact/create_dashboard
    are blocked."""
    assert dashboard_antitools_should_block("build me a dashboard", []) is True
    assert dashboard_antitools_should_block("make a sales dashboard", None) is True


def test_antitools_block_counts_artifact_calls_as_not_build(fullstack_on):
    """An anti-tool call that somehow ran earlier does NOT satisfy the build
    requirement — blocking stays active."""
    executed = ["create_artifact", "execute_query"]
    assert dashboard_antitools_should_block("build me a dashboard", executed) is True


def test_antitools_block_off_after_build(fullstack_on):
    """After create_fullstack_dashboard ran, static export is legitimate."""
    executed = ["create_fullstack_dashboard", "describe_schema"]
    assert dashboard_antitools_should_block("build me a dashboard", executed) is False


def test_antitools_block_inert_for_non_dashboard(fullstack_on):
    assert dashboard_antitools_should_block("summarize the report", []) is False
    assert dashboard_antitools_should_block("", []) is False
    assert dashboard_antitools_should_block(None, []) is False


def test_antitools_block_inert_when_flags_off(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    assert dashboard_antitools_should_block("build me a dashboard", []) is False


def test_antitools_block_active_in_legacy_mode(monkeypatch):
    """Legacy mode: the guard is active (create_artifact still blocked), but
    the v3 loop must exclude the active build tool (create_dashboard itself)
    from the blocked set — covered at the interception layer."""
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", True)
    assert dashboard_build_tool() == "create_dashboard"
    assert dashboard_antitools_should_block("build me a dashboard", []) is True


def test_antitools_constant():
    assert DASHBOARD_ANTITOOLS == frozenset({"create_artifact", "create_dashboard"})


def test_antitools_disjoint_from_build_and_guard(fullstack_on):
    """The anti-tool set must never contain the active build tool and must
    stay disjoint from the classic guard's blocked set."""
    assert "create_fullstack_dashboard" not in DASHBOARD_ANTITOOLS
    assert not (DASHBOARD_ANTITOOLS & dashboard_guard_blocked_tools())


# ── Fix 3: total-exploration cap ────────────────────────────────────────────

def test_exploration_cap_fires_at_threshold(fullstack_on):
    executed = ["describe_schema"] + ["execute_query"] * 7
    assert dashboard_exploration_cap_reached("build me a dashboard", executed, 8) is True


def test_exploration_cap_not_fired_below_threshold(fullstack_on):
    executed = ["describe_schema"] + ["execute_query"] * 6
    assert dashboard_exploration_cap_reached("build me a dashboard", executed, 8) is False


def test_exploration_cap_counts_mixed_tools(fullstack_on):
    """describe_schema + execute_sql + sql_query all count toward the same cap."""
    executed = ["describe_schema", "describe_schema", "execute_sql", "sql_query"]
    assert dashboard_exploration_cap_reached("build me a dashboard", executed, 4) is True
    assert dashboard_exploration_cap_reached("build me a dashboard", executed, 5) is False


def test_exploration_cap_ignores_non_exploration_tools(fullstack_on):
    executed = ["execute_query"] * 6 + ["web_search", "read_file", "uiux_design_system"]
    assert dashboard_exploration_cap_reached("build me a dashboard", executed, 8) is False


def test_exploration_cap_off_when_flag_zero(fullstack_on):
    executed = ["execute_query"] * 20
    assert dashboard_exploration_cap_reached("build me a dashboard", executed, 0) is False


def test_exploration_cap_inert_for_non_dashboard(fullstack_on):
    executed = ["execute_query"] * 10
    assert dashboard_exploration_cap_reached("summarize the weekly report", executed, 8) is False
    assert dashboard_exploration_cap_reached("", executed, 8) is False
    assert dashboard_exploration_cap_reached(None, executed, 8) is False


def test_exploration_cap_off_after_build(fullstack_on):
    executed = ["describe_schema", "create_fullstack_dashboard", "execute_query"] * 5
    assert dashboard_exploration_cap_reached("build me a dashboard", executed, 8) is False


def test_exploration_cap_inert_when_flags_off(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", False)
    executed = ["execute_query"] * 10
    assert dashboard_exploration_cap_reached("build me a dashboard", executed, 8) is False


def test_exploration_cap_active_in_legacy_mode(monkeypatch):
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    monkeypatch.setattr(settings, "LEGACY_DASHBOARD_ENABLED", True)
    executed = ["execute_query"] * 8
    assert dashboard_exploration_cap_reached("build me a dashboard", executed, 8) is True


def test_exploration_cap_empty_executed_list(fullstack_on):
    assert dashboard_exploration_cap_reached("build me a dashboard", [], 8) is False
    assert dashboard_exploration_cap_reached("build me a dashboard", None, 8) is False


def test_exploration_tools_constant():
    assert DASHBOARD_EXPLORATION_TOOLS == frozenset({
        "describe_schema", "execute_query", "execute_sql", "sql_query",
        "fetch_data_batch",
    })


def test_exploration_tools_include_schema_cap_tools(fullstack_on):
    """Fix 3 is a superset of T12's cap set — describe_schema is counted by
    both (whichever cap trips first)."""
    from app.services.dashboard_turn_guard import DASHBOARD_SCHEMA_CAP_TOOLS

    assert DASHBOARD_SCHEMA_CAP_TOOLS <= DASHBOARD_EXPLORATION_TOOLS


def test_exploration_tools_disjoint_from_build(fullstack_on):
    assert "create_fullstack_dashboard" not in DASHBOARD_EXPLORATION_TOOLS
    assert "create_dashboard" not in DASHBOARD_EXPLORATION_TOOLS


# ── Fix 4: duplicate artifact title parsing ─────────────────────────────────

def test_parse_artifact_title_from_title_field():
    assert parse_artifact_title('{"title": "  Sales Report  ", "description": "x"}') == "sales report"


def test_parse_artifact_title_from_name_field():
    assert parse_artifact_title('{"name": "Quarterly KPI"}') == "quarterly kpi"


def test_parse_artifact_title_title_precedence():
    assert parse_artifact_title('{"title": "Main", "name": "Other"}') == "main"


def test_parse_artifact_title_blank_fields():
    assert parse_artifact_title('{"title": "   ", "name": ""}') is None
    assert parse_artifact_title('{"title": "", "name": null}') is None


def test_parse_artifact_title_malformed_json():
    assert parse_artifact_title("{not valid json") is None
    assert parse_artifact_title("") is None
    assert parse_artifact_title(None) is None


def test_parse_artifact_title_non_dict():
    assert parse_artifact_title("[]") is None
    assert parse_artifact_title('"just a string"') is None


def test_parse_artifact_title_missing_fields():
    assert parse_artifact_title('{"description": "no title here"}') is None
