"""Regression guard for the pending-action remediation (seq-stamp API).

Bug: after "Let me re-plan..." the assistant would call a query tool that
returned only metadata (or zero rows) — the old turn-scoped
_announced_executed flip stayed True from an earlier query, so the
pending-action remediation silently stopped firing and the turn ended with
a dead metadata answer.

Fix: sequence-stamp tracking. A pending phrase fires iff _armed_seq >
_executed_seq. NO_DATA tool calls don't stamp _executed_seq, so the
remediation still fires after "Let me re-plan..." + a dead query.
"""

from __future__ import annotations

import pytest

from app.services.goal_contract import (
    RESULT_QUALITY_ASSUMED_OK,
    RESULT_QUALITY_NO_DATA,
    GoalContract,
    build_goal_contract,
)

PENDING = "let me query the sales data now"


def _contract_with_pending() -> GoalContract:
    c = build_goal_contract("what were our total shipments last month")
    c.refresh_pending_action(PENDING)
    return c


# ── the regression itself ─────────────────────────────────────────────────


def test_pending_action_fires_after_no_data_query_call() -> None:
    """Even though a query tool WAS called, a no-data result must not stamp
    _executed_seq — remediation still fires on exit."""
    c = _contract_with_pending()
    c.record_tool_executed("ask_data_agent", result_quality=RESULT_QUALITY_NO_DATA)
    crits = c.unmet(granted_tools={"ask_data_agent"})
    assert crits, "pending-action remediation must still fire"
    codes = {cr.code for cr in crits}
    assert "pending_action" in codes


def test_unmet_pending_action_directly_after_no_data() -> None:
    """Unit-level: _unmet_pending_action itself fires after a no_data call."""
    c = _contract_with_pending()
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_NO_DATA)
    assert c._executed_seq == 0, "NO_DATA must not stamp _executed_seq"
    crits = c.unmet(granted_tools={"execute_query"})
    assert any(cr.code == "pending_action" for cr in crits)


# ── backward compat: usable results still satisfy the promise ──────────────


def test_pending_action_satisfied_after_usable_query() -> None:
    c = _contract_with_pending()
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product_name": "Widget", "total_revenue": 100}])
    # After the query executes, _executed_seq >= _armed_seq
    assert c._executed_seq >= c._armed_seq
    assert c.unmet(granted_tools={"execute_query"}) == []


def test_pending_action_satisfied_after_artifact_call() -> None:
    """Artifact calls are not queries but they stamp _executed_seq
    (default quality assumed_ok)."""
    c = _contract_with_pending()
    c.record_tool_executed("create_artifact")
    assert c._executed_seq >= c._armed_seq
    assert c.unmet(granted_tools={"create_artifact"}) == []


def test_pending_action_satisfied_after_plain_query_default_quality() -> None:
    """record_tool_executed without an explicit quality keeps legacy behavior
    (default param = assumed_ok)."""
    c = _contract_with_pending()
    c.record_tool_executed("execute_query")
    assert c._executed_seq >= c._armed_seq
    assert c.unmet(granted_tools={"execute_query"}) == []


# ── quality flag propagation semantics ────────────────────────────────────


def test_no_data_quality_does_not_stamp_execution() -> None:
    c = _contract_with_pending()
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_NO_DATA)
    assert c._executed_seq == 0


def test_later_usable_query_stamps_execution() -> None:
    """no_data then real data: the usable call stamps _executed_seq, so the
    pending action is considered fulfilled again."""
    c = _contract_with_pending()
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_NO_DATA)
    c.record_tool_executed("execute_query", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product_name": "Widget", "total_revenue": 100}])
    assert c._executed_seq >= c._armed_seq
    assert c.unmet(granted_tools={"execute_query"}) == []


def test_non_query_tool_does_not_stamp_execution() -> None:
    c = _contract_with_pending()
    c.record_tool_executed("describe_schema")  # not in QUERY/ARTIFACT/DASHBOARD
    assert c._executed_seq == 0
    assert any(cr.code == "pending_action" for cr in c.unmet(granted_tools={"execute_query"}))
