"""Dynamic turn complexity — ReAct-style budget by intention and workload.

User mandate (2026-10-20): "make time dynamic, according to user intention
and according to work". A fixed wall-clock timeout is wrong for agents: big
or complex data legitimately needs longer, while a simple lookup should not
hang for minutes.

Contract pinned here:
- every turn is classified into a complexity tier (deterministic keyword
  heuristic — no LLM call): quick / standard / deep;
- the wall-clock target and iteration budget scale with the tier;
- the loop may EXTEND the wall budget only while it makes NEW progress
  (a tool call with arguments not used before this turn) — ReAct's "end the
  loop when the answer condition is met", implemented as "buy time for
  genuine exploration, wrap up on stagnation";
- a stagnant loop is never extended, and the cap never truncates an
  in-flight answer — it only moves the loop to post-loop synthesis.
"""

from __future__ import annotations

import json
import os

from app.services.agent_prompts import (
    _TURN_TIER_ITERS,
    _TURN_TIER_WALL_S,
    calculate_agent_budget,
    classify_turn_complexity,
)
from app.routers.agents import _call_sig, _classify_turn_tier

_AGENTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "routers", "agents.py"
)


class TestTierClassification:
    def test_simple_lookup_is_quick(self):
        assert classify_turn_complexity("how much revenue last week?")[
            "tier"] == "quick"
        assert classify_turn_complexity("what was the order count today")[
            "tier"] == "quick"

    def test_overview_is_deep(self):
        # The live turn: 215s, multi-dimension executive summary.
        assert classify_turn_complexity(
            "give me last week sales overview"
        )["tier"] == "deep"

    def test_multi_part_request_is_deep(self):
        assert classify_turn_complexity(
            "breakdown by product and customer with trend"
        )["tier"] == "deep"

    def test_comparative_question_is_deep(self):
        assert classify_turn_complexity(
            "compare this month versus last month"
        )["tier"] == "deep"

    def test_unmatched_question_is_standard(self):
        assert classify_turn_complexity(
            "open the supplier record for ACME"
        )["tier"] == "standard"

    def test_tiers_have_targets_and_budgets(self):
        assert _TURN_TIER_WALL_S["quick"] < _TURN_TIER_WALL_S["standard"]
        assert _TURN_TIER_WALL_S["standard"] < _TURN_TIER_WALL_S["deep"]
        assert _TURN_TIER_ITERS["quick"] <= _TURN_TIER_ITERS["standard"]
        assert _TURN_TIER_ITERS["standard"] <= _TURN_TIER_ITERS["deep"]

    def test_automation_escalates_to_deep_when_already_deep(self):
        assert classify_turn_complexity(
            "comprehensive sales analysis", is_automation=True
        )["tier"] == "deep"


class TestBudgetIntegration:
    def test_deep_question_gets_larger_iteration_budget(self):
        deep = calculate_agent_budget(
            None, "sales overview by product and customer")
        std = calculate_agent_budget(None, "open the supplier record")
        assert deep > std

    def test_helper_maps_tier_to_settings_target(self):
        info = _classify_turn_tier("how much revenue last week?", False)
        assert info["tier"] == "quick"
        assert info["wall_target_s"] == 100.0
        info = _classify_turn_tier("comprehensive sales overview", False)
        assert info["wall_target_s"] == 330.0


class TestCallSignature:
    def test_same_call_same_signature_regardless_of_arg_order(self):
        a = {"function": {"name": "query_database",
                          "arguments": '{"sql": "SELECT 1", "limit": 10}'}}
        b = {"function": {"name": "query_database",
                          "arguments": '{"limit": 10, "sql": "SELECT 1"}'}}
        assert _call_sig(a) == _call_sig(b)

    def test_different_args_different_signature(self):
        a = {"function": {"name": "query_database",
                          "arguments": '{"sql": "SELECT 1"}'}}
        b = {"function": {"name": "query_database",
                          "arguments": '{"sql": "SELECT 2"}'}}
        assert _call_sig(a) != _call_sig(b)

    def test_new_window_is_new_progress(self):
        """Re-running the same query for a different date window IS new
        exploration and must qualify for a wall-budget extension."""
        a = _call_sig({"function": {"name": "ask_data_agent",
                                    "arguments": '{"question": "Oct 5-11 sales"}'}})
        b = _call_sig({"function": {"name": "ask_data_agent",
                                    "arguments": '{"question": "Oct 12-18 sales"}'}})
        assert a != b

    def test_unparsable_args_do_not_crash(self):
        assert _call_sig({"function": {"name": "x"}})


class TestLoopWiring:
    """Structural pins: the v3 loop extends only on new progress, never
    blindly, and never mid-answer."""

    def _source(self) -> str:
        with open(_AGENTS_PATH) as f:
            return f.read()

    def test_wall_check_has_progress_extension(self):
        src = self._source()
        assert "TURN_WALL_MAX_EXTENSIONS" in src
        assert "NEW progress" in src
        assert "_iter_new_sigs" in src

    def test_extension_only_when_new_sigs_and_budget_remains(self):
        src = self._source()
        # The extension guard is a conjunction: new signatures AND
        # extensions remaining AND the flag on.
        assert (
            'settings.TURN_COMPLEXITY_ENABLED\n                    and _new_sigs'
            in src
        )
        assert "_wall_extensions_used < _wall_max_extensions" in src

    def test_cap_computation_uses_tier_for_ordinary_turns(self):
        src = self._source()
        assert 'classify_turn_complexity' in src or "_classify_turn_tier" in src
        # Dashboard/CAD long caps untouched.
        assert "DASHBOARD_BUILD_WALL_CLOCK_CAP_S" in src
        assert "FUSION_BUILD_WALL_CLOCK_CAP_S" in src

    def test_sigs_recorded_at_capture_time(self):
        """Signatures are classified new/seen AT CAPTURE, so a repeated call
        can never re-qualify as progress on a later wall check."""
        src = self._source()
        assert "_seen_call_sigs.update(_last_iter_call_sigs)" in src


class TestReportDecompositionSkip:
    """A ranked/limited ask whose 'dimensions' are just the query's group-by
    columns must NOT be decomposed into per-dimension NL2SQL pipelines —
    measured live: a "top 10 customer list" ran FOUR full NL2SQL passes
    (~8 min) for a question ONE query answers."""

    def _spec(self, **overrides):
        spec = {
            "task_kind": "analyze_data",
            "requires_data": True,
            "user_message": (
                "Top 10 customers by total sales revenue for last week "
                "(2026-10-12 to 2026-10-18) from erp_v_sale_orderentry"
            ),
            "dimensions": ["customer_code", "customer_name"],
            "entities": {
                "group_by": ["customer_code", "customer_name"],
                "limit": 10,
                "date_range": {"start": "2026-10-12", "end": "2026-10-18"},
            },
        }
        spec.update(overrides)
        return spec

    def test_ranked_ask_with_groupby_dimensions_skips_decomposition(self):
        from app.services.synexia.plan_dag import (
            _build_report_decomposition_steps,
        )
        assert _build_report_decomposition_steps(self._spec()) is None

    def test_top_n_without_limit_field_also_skips(self):
        from app.services.synexia.plan_dag import (
            _build_report_decomposition_steps,
        )
        spec = self._spec()
        spec["entities"] = {"group_by": ["customer_code", "customer_name"]}
        assert _build_report_decomposition_steps(spec) is None

    def test_genuine_multi_dimension_report_still_decomposes(self):
        from app.services.synexia.plan_dag import (
            _build_report_decomposition_steps,
        )
        spec = self._spec(
            user_message=(
                "give me a sales report broken down by product, customer "
                "and region for last week"
            ),
            dimensions=["product", "customer", "region"],
            entities={"group_by": [], "limit": None},
        )
        steps = _build_report_decomposition_steps(spec)
        assert steps is not None and len(steps) >= 4
        assert any(s["node_type"] == "synthesize" for s in steps)

    def test_decomposition_never_when_no_dimension_overlap_is_requested(self):
        """Ranked ask but dimensions differ from group-by (the planner wants
        EXTRA dimensions) — decomposition still applies."""
        from app.services.synexia.plan_dag import (
            _build_report_decomposition_steps,
        )
        spec = self._spec(
            user_message="top 10 customers sales broken down by region",
            dimensions=["region"],
            entities={"group_by": ["customer_code"], "limit": 10},
        )
        steps = _build_report_decomposition_steps(spec)
        assert steps is not None
