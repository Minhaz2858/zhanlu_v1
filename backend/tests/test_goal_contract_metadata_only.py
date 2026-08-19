"""Tests for the GoalContract metadata-only wiring.

Covers: ``metadata_only_events`` counter updates in ``record_query_result``,
the ``_unmet_metadata_only`` criterion, and its priority inside ``unmet()``.
These are shape-level tests (plain dicts) — cross-database integration lives
in ``test_data_agent_structured_failure.py`` / ``test_schema_validator_cross_db.py``.
"""

from __future__ import annotations

import pytest

from app.services.goal_contract import GoalContract, build_goal_contract


# ── counter updates in record_query_result ────────────────────────────────


def test_record_metadata_only_rows_increments_counter() -> None:
    c = GoalContract()
    c.record_query_result([{"MIN_FDATE": "2026-01-01", "MAX_FDATE": "2026-08-19"}])
    assert c.metadata_only_events == 1
    assert c.zero_row_events == 0  # metadata carries signal → not "empty"


def test_record_normal_rows_resets_metadata_counter() -> None:
    c = GoalContract()
    c.record_query_result([{"MIN_FDATE": "2026-01-01"}])
    assert c.metadata_only_events == 1
    c.record_query_result([{"product_name": "Widget", "total_revenue": 100}])
    assert c.metadata_only_events == 0


def test_record_empty_rows_does_not_increment_metadata_counter() -> None:
    c = GoalContract()
    c.record_query_result([])
    assert c.zero_row_events == 1
    assert c.metadata_only_events == 0


def test_metadata_only_all_null_also_counts_empty() -> None:
    """A metadata-only snapshot whose values are all None is BOTH empty and
    metadata-only — zero_rows wins priority at exit, but both counters move."""
    c = GoalContract()
    c.record_query_result([{"MIN_FDATE": None, "MAX_FDATE": None}])
    assert c.zero_row_events == 1
    assert c.metadata_only_events == 1


# ── unmet / remediation force ─────────────────────────────────────────────


def test_unmet_metadata_only_fires_with_query_tool() -> None:
    c = build_goal_contract("what were our total shipments last month")
    assert c.requires_data and c.expects_rows
    c.record_query_result([{"count_rows": 3}])
    crits = c.unmet(granted_tools={"execute_query", "create_artifact"})
    assert len(crits) == 1
    crit = crits[0]
    assert crit.code == "metadata_only"
    assert crit.force_tool == "execute_query"
    assert "MIN/MAX/COUNT" in crit.message


def test_unmet_metadata_only_requires_data_flag() -> None:
    c = GoalContract()  # no requires_data / expects_rows
    c.record_query_result([{"count_rows": 3}])
    assert c.metadata_only_events == 1
    assert c.unmet(granted_tools={"execute_query"}) == []


def test_metadata_only_priority_over_pending_action() -> None:
    c = build_goal_contract("what were our total shipments last month")
    c.pending_action_phrase = "let me query the data now"  # announced, not executed
    c.record_query_result([{"MIN_FDATE": "2026-01-01"}])
    crits = c.unmet(granted_tools={"execute_query"})
    assert crits and crits[0].code == "metadata_only"


def test_metadata_only_after_zero_rows_still_fires() -> None:
    """A failed query (zero rows) followed by a metadata-only snapshot must
    still trigger remediation — the metadata-only counter is independent."""
    c = build_goal_contract("what were our total shipments last month")
    c.record_query_result([])  # zero rows
    assert c.unmet(granted_tools={"execute_query"})[0].code == "zero_rows"
    c.record_query_result([{"MAX_FDATE": "2026-08-19"}])  # metadata-only
    assert c.metadata_only_events == 1
    crits = c.unmet(granted_tools={"execute_query"})
    assert crits[0].code == "metadata_only"


def test_unmet_metadata_only_respects_force_budget() -> None:
    c = build_goal_contract("what were our total shipments last month")
    c.max_forces = 3
    c.forces_used = 3
    c.record_query_result([{"count_rows": 3}])
    assert c.unmet(granted_tools={"execute_query"}) == []


def test_unmet_metadata_only_no_query_tool() -> None:
    c = build_goal_contract("what were our total shipments last month")
    c.record_query_result([{"MIN_FDATE": "2026-01-01"}])
    assert c.unmet(granted_tools={"create_artifact"}) == []


def test_contract_satisfied_after_real_data_resets_metadata() -> None:
    c = build_goal_contract("what were our total shipments last month")
    c.record_query_result([{"MIN_FDATE": "2026-01-01"}])
    assert c.satisfied(granted_tools={"execute_query"}) is False
    c.record_query_result([{"product_name": "Widget", "total_revenue": 100}])
    assert c.satisfied(granted_tools={"execute_query"}) is True
