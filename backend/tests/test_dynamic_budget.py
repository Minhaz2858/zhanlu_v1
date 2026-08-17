"""Tests for the dynamic per-turn iteration budget.

Verifies `calculate_agent_budget`:
- Base budget is 4 for simple single-table queries.
- +2 per high-confidence related-table edge (confidence >= 0.8).
- Low-confidence edges are ignored.
- +2 for multi-table intent (dashboard / report / join keywords).
- +2 for automation runs (no human to ask).
- Hard cap at 10.
And the `_schema_edge_count` helper in app.routers.agents, which turns a
describe_schema schema-graph result into the edge-count signal used to
UPGRADE the budget mid-loop.
"""

from __future__ import annotations

from app.services.agent_prompts import calculate_agent_budget


def _edge(confidence: float) -> dict:
    return {"table": "a", "target_table": "b", "confidence": confidence}


class TestCalculateAgentBudget:
    def test_base_budget_for_simple_query(self):
        assert calculate_agent_budget() == 4
        assert calculate_agent_budget(None, "") == 4
        assert calculate_agent_budget([], "how many orders today?", False) == 4

    def test_high_confidence_edge_adds_two(self):
        assert calculate_agent_budget([_edge(0.9)]) == 6

    def test_two_high_confidence_edges_add_four(self):
        assert calculate_agent_budget([_edge(0.9), _edge(0.95)]) == 8

    def test_low_confidence_edges_are_ignored(self):
        # 0.5 NAME_MATCH edges must NOT expand the budget — they are hints,
        # not join proof.
        assert calculate_agent_budget([_edge(0.5), _edge(0.6)]) == 4

    def test_edge_at_exactly_0_8_counts(self):
        assert calculate_agent_budget([_edge(0.8)]) == 6

    def test_edge_below_0_8_does_not_count(self):
        assert calculate_agent_budget([_edge(0.79)]) == 4

    def test_multi_table_keyword_adds_two(self):
        assert calculate_agent_budget(None, "build me a dashboard") == 6
        assert calculate_agent_budget(None, "show the full report") == 6
        assert calculate_agent_budget(None, "join orders and shipments") == 6

    def test_automation_run_adds_two(self):
        assert calculate_agent_budget(None, "any question", True) == 6

    def test_combined_scenario_capped_at_ten(self):
        # 2 high-confidence edges (+4) + multi-table keyword (+2) +
        # automation (+2) = 12 → capped at 10.
        budget = calculate_agent_budget(
            [_edge(0.9), _edge(0.9)],
            "consolidated overview for the morning report",
            is_automation=True,
        )
        assert budget == 10

    def test_never_below_minimum(self):
        assert calculate_agent_budget([], "", False) >= 4

    def test_never_above_maximum(self):
        edges = [_edge(0.9) for _ in range(20)]
        assert calculate_agent_budget(edges, "dashboard", True) <= 10

    def test_user_question_lowercase_is_case_insensitive(self):
        assert calculate_agent_budget(None, "Build A DASHBOARD") == 6


class TestSchemaEdgeCount:
    """The mid-loop budget upgrade signal parsed from describe_schema output."""

    def _schema_text(self, edge_lines: list[str]) -> str:
        header = (
            "TABLE orders\n  columns: id, customer_id, order_date\n"
            "RELATED TABLES (auto-join candidates)\n"
        )
        return header + "\n".join(edge_lines)

    def test_counts_high_confidence_edges(self):
        from app.routers.agents import _schema_edge_count

        result = {
            "success": True,
            "source": "schema_graph",
            "schema": self._schema_text(
                [
                    "    - customers via customer_id -> id (FK, conf=1.00)",
                    "    - regions via region_code -> code (VALUE_OVERLAP, conf=0.90)",
                    "    - products via product_name -> name (NAME_MATCH, conf=0.50)",
                ]
            ),
        }
        # FK + VALUE_OVERLAP >= 0.8 → 2; NAME_MATCH 0.5 → ignored.
        assert _schema_edge_count(result) == 2

    def test_returns_zero_for_non_schema_graph_source(self):
        from app.routers.agents import _schema_edge_count

        assert _schema_edge_count({"success": True, "schema": "plain ddl"}) == 0
        assert _schema_edge_count(None) == 0
        assert _schema_edge_count("not a dict") == 0

    def test_returns_zero_when_no_edge_lines(self):
        from app.routers.agents import _schema_edge_count

        result = {
            "success": True,
            "source": "schema_graph",
            "schema": "TABLE orders\n  columns: id\n",
        }
        assert _schema_edge_count(result) == 0

    def test_sample_rows_do_not_skew_the_count(self):
        from app.routers.agents import _schema_edge_count

        # Sample-row lines start with "- " but carry no conf= markers.
        result = {
            "success": True,
            "source": "schema_graph",
            "schema": (
                "TABLE orders\n  columns: id, customer_id\n"
                "RELATED TABLES (auto-join candidates)\n"
                "    - customers via customer_id -> id (FK, conf=1.00)\n"
                "SAMPLE ROWS (orders)\n"
                "    - id=1 customer_id=101\n"
                "    - id=2 customer_id=102\n"
            ),
        }
        assert _schema_edge_count(result) == 1


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
