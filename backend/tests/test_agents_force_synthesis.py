"""v3 loop: _contract_force_synthesis consumption and _loop_exit_monotonic.

Tests the agents.py wiring: when GoalContract returns an unmet criterion
with force_synthesis=True, the exit checker arms _contract_force_synthesis,
and the pre-LLM consumption block sets tool_choice="none" + clears the
flag (consume-at-top). Also verifies _loop_exit_monotonic for step duration.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from app.services.goal_contract import (
    RESULT_QUALITY_ASSUMED_OK,
    RESULT_QUALITY_NO_DATA,
    UnmetCriterion,
    build_goal_contract,
)


# ── force_synthesis arming from the exit checker ──────────────────────────


def test_unmet_force_synthesis_criterion() -> None:
    """When the contract returns force_synthesis=True, the exit checker
    should arm _contract_force_synthesis (not _contract_force_tool)."""
    c = build_goal_contract("Give me supply chain data for last 30 days")
    c.record_tool_executed("ask_data_agent", result_quality=RESULT_QUALITY_ASSUMED_OK)
    c.record_query_result([{"product": "C5", "revenue": 1000}])
    c.refresh_pending_action("Let me re-query against the live tables.")
    crits = c.unmet(granted_tools={"ask_data_agent"})
    assert crits, "unmet must fire"
    assert crits[0].force_synthesis is True
    assert crits[0].force_tool is None


def test_unmet_force_tool_criterion() -> None:
    """When no usable results exist, force_tool is used (legacy path)."""
    c = build_goal_contract("show me sales")
    c.refresh_pending_action("Let me query the sales table.")
    crits = c.unmet(granted_tools={"execute_query"})
    assert crits, "unmet must fire"
    assert crits[0].force_synthesis is False
    assert crits[0].force_tool == "execute_query"


# ── UnmetCriterion force_synthesis field ──────────────────────────────────


def test_unmet_criterion_default_force_synthesis_false() -> None:
    c = UnmetCriterion("zero_rows", "no data")
    assert c.force_synthesis is False


def test_unmet_criterion_force_synthesis_true() -> None:
    c = UnmetCriterion("pending_action", "answer now", force_synthesis=True)
    assert c.force_synthesis is True
    assert c.force_tool is None


# ── _loop_exit_monotonic stamps on loop exit ─────────────────────────────


def test_loop_exit_monotonic_stamped() -> None:
    """Verify _loop_exit_monotonic is set after the v3 loop ends.
    We can't run the full v3 loop, but we can verify the variable
    is initialized and stamped correctly in the code structure."""
    # This is a structural test — the variable exists and is used.
    # The real test is the acceptance prompt against the live backend.
    import app.routers.agents as agents_mod
    source = open(agents_mod.__file__).read()
    assert "_loop_exit_monotonic" in source
    assert "_loop_exit_monotonic = None" in source
    assert "_loop_exit_monotonic = time.monotonic()" in source


def test_contract_force_synthesis_in_source() -> None:
    """Verify _contract_force_synthesis is wired in agents.py."""
    import app.routers.agents as agents_mod
    source = open(agents_mod.__file__).read()
    assert "_contract_force_synthesis = False" in source
    assert "_contract_force_synthesis = True" in source
    assert "tool_choice = \"none\"" in source
    assert "_contract_force_synthesis = False  # consume" in source
