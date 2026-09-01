import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.dashboard_turn_guard import (
    is_live_dashboard_request,
    should_force_create_dashboard,
    dashboard_guard_blocked_tools,
)


def _tc(name, success=True):
    return {"name": name, "status": "completed" if success else "failed", "results": {"success": success}}


def test_live_dashboard_request_detects_dashboard_even_with_negated_html():
    assert is_live_dashboard_request("Build a live sales dashboard, not HTML") is True


def test_live_dashboard_request_ignores_static_html_request():
    assert is_live_dashboard_request("Build an HTML report page") is False


def test_live_dashboard_request_detects_chinese_keywords():
    assert is_live_dashboard_request("做一个仪表盘") is True
    assert is_live_dashboard_request("给我看下数据看板") is True
    assert is_live_dashboard_request("搞个数据面板") is True


def test_force_create_dashboard_after_design_source_and_schema():
    tool_calls = [
        _tc("uiux_design_system"),
        _tc("list_data_sources"),
        _tc("describe_schema"),
    ]
    assert should_force_create_dashboard("make dashboard", tool_calls) is True


def test_force_create_dashboard_does_not_wait_for_exploratory_queries():
    tool_calls = [_tc("uiux_search"), _tc("list_data_sources"), _tc("describe_schema")]
    assert should_force_create_dashboard("make sales dashboard", tool_calls) is True


def test_force_create_dashboard_false_after_create_dashboard():
    tool_calls = [
        _tc("list_data_sources"),
        _tc("describe_schema"),
        _tc("execute_query"),
        _tc("create_dashboard"),
    ]
    assert should_force_create_dashboard("make dashboard", tool_calls) is False


def test_force_create_dashboard_false_without_dashboard_intent():
    tool_calls = [_tc("list_data_sources"), _tc("describe_schema"), _tc("execute_query"), _tc("execute_query")]
    assert should_force_create_dashboard("summarize sales", tool_calls) is False


def test_speculative_force_create_dashboard_in_bi_project_on_greeting():
    """The agent in an Ecisco BI chat speculatively loads dashboard skills
    after the user says 'hi'. Once it has done describe_schema + a design
    tool, force create_dashboard so the user sees an actual dashboard."""
    tool_calls = [
        _tc("uiux_design_system"),
        _tc("describe_schema"),
    ]
    # Explicit dashboard request is False (user just said "hi")
    assert is_live_dashboard_request("hi") is False
    # But in a BI project with schema + design done, force it
    assert should_force_create_dashboard(
        "hi",
        tool_calls,
        is_dashboard_project=True,
    ) is True


def test_speculative_force_false_without_design_or_schema():
    tool_calls = [_tc("describe_schema")]
    assert should_force_create_dashboard(
        "hi",
        tool_calls,
        is_dashboard_project=True,
    ) is False


def test_speculative_force_false_in_non_dashboard_project():
    """Without the BI project flag, the speculative path doesn't fire —
    a non-BI agent that loads skills shouldn't be forced to make a dashboard."""
    tool_calls = [_tc("uiux_design_system"), _tc("describe_schema")]
    assert should_force_create_dashboard(
        "hi",
        tool_calls,
        is_dashboard_project=False,
    ) is False


def test_speculative_force_false_when_dashboard_tool_missing():
    tool_calls = [_tc("uiux_design_system"), _tc("describe_schema")]
    assert should_force_create_dashboard(
        "hi",
        tool_calls,
        has_dashboard_tool=False,
        is_dashboard_project=True,
    ) is False


def test_dashboard_guard_blocks_execute_query():
    """The interception layer blocks execute_query/sql_query when the guard
    fires so the LLM can't dodge the forced create_dashboard call."""
    blocked = dashboard_guard_blocked_tools()
    assert "execute_query" in blocked
    assert "execute_sql" in blocked
    assert "sql_query" in blocked
    assert "create_dashboard" not in blocked
    assert "describe_schema" not in blocked
