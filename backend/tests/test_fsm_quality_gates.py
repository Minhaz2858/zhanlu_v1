"""Tests for the new FSM quality gates in synexia.verifier.

Verifies:
1. _check_degenerate_result — all-NULL or 0-row datasets flagged
2. _check_wrong_grain — aggregate expected but got raw rows flagged
3. _check_coverage — missing plan node coverage flagged
4. _BOUNCE_BACK_PATTERN_RE — bounce-back text detected
5. _heuristic_verdict bounce-back detection in quality_eval
"""

from __future__ import annotations

import re
import unittest
from dataclasses import dataclass, field
from typing import Optional

from app.services.synexia.verifier import (
    _check_degenerate_result,
    _check_wrong_grain,
    _check_coverage,
)
from app.services.synexia.quality_eval import _heuristic_verdict, _BOUNCE_BACK_MARKERS


# ── Fakes ────────────────────────────────────────────────────────────────────

@dataclass
class _FakePlanNode:
    id: str = "node-1"
    optional: bool = False
    produces_data: bool = True
    expected_grain: Optional[str] = None


@dataclass
class _FakeObservation:
    id: str = "obs-1"
    tool_name: Optional[str] = None
    success: bool = True
    result_data: Optional[dict] = None
    artifact_ids: list = field(default_factory=list)
    plan_node: Optional[_FakePlanNode] = None
    plan_node_id: Optional[str] = None
    node_id: Optional[str] = None


# ── Degenerate-result tests ─────────────────────────────────────────────────

class TestDegenerateResult(unittest.TestCase):
    def test_effectively_empty_rows_flagged(self):
        """Rows with all-NULL values → degenerate."""
        obs = _FakeObservation(
            tool_name="ask_data_agent",
            result_data={"rows": [{"a": None, "b": None}]},
        )
        result = _check_degenerate_result([obs])
        self.assertFalse(result["ok"])
        self.assertTrue(result["critical"])

    def test_zero_rows_flagged(self):
        """0 rows returned → degenerate."""
        obs = _FakeObservation(
            tool_name="ask_data_agent",
            result_data={"rows": []},
        )
        result = _check_degenerate_result([obs])
        self.assertFalse(result["ok"])

    def test_valid_data_passes(self):
        """Rows with actual values → ok."""
        obs = _FakeObservation(
            tool_name="ask_data_agent",
            result_data={"rows": [{"a": 1, "b": "hello"}]},
        )
        result = _check_degenerate_result([obs])
        self.assertTrue(result["ok"])

    def test_mixed_null_and_value_passes(self):
        """Rows where some values are non-null → ok (not degenerate)."""
        obs = _FakeObservation(
            tool_name="ask_data_agent",
            result_data={"rows": [{"a": None, "b": "2026-08-20"}]},
        )
        result = _check_degenerate_result([obs])
        self.assertTrue(result["ok"])

    def test_non_data_agent_observation_ignored(self):
        """Observations without 'rows' key are not checked for degenerate data."""
        obs = _FakeObservation(
            tool_name="web_search",
            result_data={"answer": "some text"},
        )
        result = _check_degenerate_result([obs])
        self.assertTrue(result["ok"])

    def test_failed_observation_skipped(self):
        """Failed observations are skipped."""
        obs = _FakeObservation(
            success=False,
            tool_name="ask_data_agent",
            result_data={"rows": []},
        )
        result = _check_degenerate_result([obs])
        self.assertTrue(result["ok"])


# ── Wrong-grain tests ────────────────────────────────────────────────────────

class TestWrongGrain(unittest.TestCase):
    def test_aggregate_expected_got_raw_rows(self):
        """Plan says aggregate, got 60 raw rows → wrong grain."""
        obs = _FakeObservation(
            result_data={"rows": [{"a": i} for i in range(60)]},
            plan_node=_FakePlanNode(expected_grain="aggregate"),
        )
        result = _check_wrong_grain([obs])
        self.assertFalse(result["ok"])
        self.assertFalse(result["critical"])  # soft warning

    def test_aggregate_expected_few_rows_ok(self):
        """Plan says aggregate, got 3 rows → ok."""
        obs = _FakeObservation(
            result_data={"rows": [{"a": 1}, {"a": 2}, {"a": 3}]},
            plan_node=_FakePlanNode(expected_grain="aggregate"),
        )
        result = _check_wrong_grain([obs])
        self.assertTrue(result["ok"])

    def test_raw_grain_ok(self):
        """Plan says raw, many rows → ok."""
        obs = _FakeObservation(
            result_data={"rows": [{"a": i} for i in range(300)]},
            plan_node=_FakePlanNode(expected_grain="raw"),
        )
        result = _check_wrong_grain([obs])
        self.assertTrue(result["ok"])

    def test_no_grain_spec_many_rows_flagged(self):
        """No grain specified, >200 rows without pagination → likely wrong grain."""
        obs = _FakeObservation(
            result_data={"rows": [{"a": i} for i in range(250)]},
        )
        result = _check_wrong_grain([obs])
        self.assertFalse(result["ok"])


# ── Coverage tests ───────────────────────────────────────────────────────────

class TestCoverage(unittest.TestCase):
    def test_all_nodes_covered(self):
        """Every plan node has data → ok."""
        obs = _FakeObservation(
            node_id="node-1",
            result_data={"rows": [{"a": 1}]},
        )
        node = _FakePlanNode(id="node-1")
        result = _check_coverage([obs], [node])
        self.assertTrue(result["ok"])

    def test_missing_node_flagged(self):
        """Plan node without data → coverage failure."""
        obs = _FakeObservation(
            node_id="node-1",
            result_data={"rows": [{"a": 1}]},
        )
        missing_node = _FakePlanNode(id="node-2")
        result = _check_coverage([obs], [_FakePlanNode(id="node-1"), missing_node])
        self.assertFalse(result["ok"])
        self.assertTrue(result["critical"])

    def test_optional_node_not_required(self):
        """Optional plan nodes don't need coverage."""
        obs = _FakeObservation(
            node_id="node-1",
            result_data={"rows": [{"a": 1}]},
        )
        optional_node = _FakePlanNode(id="node-2", optional=True)
        result = _check_coverage([obs], [_FakePlanNode(id="node-1"), optional_node])
        self.assertTrue(result["ok"])

    def test_no_plan_returns_ok(self):
        """No plan nodes → trivially ok."""
        result = _check_coverage([], None)
        self.assertTrue(result["ok"])


# ── Bounce-back detection tests ──────────────────────────────────────────────

class TestBounceBackDetection(unittest.TestCase):
    def test_bounce_back_english_rows(self):
        result = _heuristic_verdict("I retrieved 1 rows from the database. You can ask me for a summary.")
        self.assertEqual(result.verdict, "revise")
        self.assertLess(result.completeness_score, 0.5)

    def test_bounce_back_english_ask_me(self):
        result = _heuristic_verdict("Data was retrieved. Ask me for a breakdown or chart.")
        self.assertEqual(result.verdict, "revise")

    def test_bounce_back_chinese(self):
        result = _heuristic_verdict("你可以让我提供一份摘要")
        self.assertEqual(result.verdict, "revise")

    def test_normal_answer_accepted(self):
        result = _heuristic_verdict(
            "In July 2026, sales reached ¥44.7M across 340 order lines. "
            "The top product was material 103350 with ¥39.4M."
        )
        self.assertEqual(result.verdict, "accept")

    def test_apology_still_detected(self):
        """Apology detection still works (not overridden by bounce-back)."""
        result = _heuristic_verdict(
            "I gathered some information but had trouble putting it all together."
        )
        self.assertEqual(result.verdict, "revise")


if __name__ == "__main__":
    unittest.main()
