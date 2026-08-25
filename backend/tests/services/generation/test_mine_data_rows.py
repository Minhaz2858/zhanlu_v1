"""Tests for _mine_ask_data_rows and _iter_historical_data_results —
the artifact-builder's data-mining functions.

Validates that the expanded DATA_PRODUCING_TOOLS set (now including
ask_erp_kpi, ask_erp_writeback, ask_perception_intelligence, ask_forecast_pricing)
is correctly scanned by the artifact builder when pulling rows for
DOCX/PPTX/dashboard generation.
"""

import json
import pytest
import sys
import os

# ── Path setup for in-container pytest ──────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/venv/lib/python3.11/site-packages")

from app.services.generation_orchestrator import (
    _mine_ask_data_rows,
    _iter_historical_data_results,
    _iter_historical_ask_data_results,
    _mine_historical_ask_data_result,
    _mine_historical_answer_rows,
)
from app.routers.agents import DATA_PRODUCING_TOOLS


# ═══════════════════════════════════════════════════════════════════
# 1. _mine_ask_data_rows — tool_calls_for_frontend scanning
# ═══════════════════════════════════════════════════════════════════

class TestMineAskDataRows:
    """_mine_ask_data_rows pulls rows from ALL DATA_PRODUCING_TOOLS."""

    def test_ask_data_agent_rows_found(self):
        """Regression: ask_data_agent still works."""
        tcs = [
            {
                "name": "ask_data_agent",
                "results": {
                    "rows": [{"product": "A", "qty": 100}, {"product": "B", "qty": 200}],
                    "sql": "SELECT * FROM sales",
                },
            }
        ]
        rows = _mine_ask_data_rows(tcs)
        assert len(rows) == 2
        assert rows[0]["product"] == "A"

    def test_ask_erp_kpi_rows_found(self):
        """New: ask_erp_kpi with normalized rows is scanned."""
        tcs = [
            {
                "name": "ask_erp_kpi",
                "results": {
                    "success": True,
                    "rows": [{"product_id": "isoprene", "inventory_tons": 1234.5}],
                    "source_name": "EDIA:ask_erp_kpi",
                },
            }
        ]
        rows = _mine_ask_data_rows(tcs)
        assert len(rows) == 1
        assert rows[0]["product_id"] == "isoprene"

    def test_execute_query_rows_found(self):
        """Regression: execute_query still works."""
        tcs = [
            {
                "name": "execute_query",
                "results": {
                    "rows": [{"month": "July", "revenue": 50000}],
                },
            }
        ]
        rows = _mine_ask_data_rows(tcs)
        assert len(rows) == 1
        assert rows[0]["month"] == "July"

    def test_mixed_tools_all_scanned(self):
        """Multiple data-producing tools in one turn — all rows collected."""
        tcs = [
            {
                "name": "ask_data_agent",
                "results": {"rows": [{"id": 1}]},
            },
            {
                "name": "ask_erp_kpi",
                "results": {"rows": [{"product_id": "dcpd"}]},
            },
            {
                "name": "execute_query",
                "results": {"rows": [{"col": "x"}]},
            },
        ]
        rows = _mine_ask_data_rows(tcs)
        assert len(rows) == 3

    def test_non_data_tool_skipped(self):
        """Non-data tools (e.g. ask_perception) without rows are skipped."""
        tcs = [
            {
                "name": "ask_perception",  # NOT in DATA_PRODUCING_TOOLS
                "results": {"success": True, "answer": "some prose"},
            },
            {
                "name": "ask_data_agent",
                "results": {"rows": [{"id": 1}]},
            },
        ]
        rows = _mine_ask_data_rows(tcs)
        assert len(rows) == 1

    def test_superseded_results_skipped(self):
        tcs = [
            {
                "name": "ask_data_agent",
                "__superseded": True,
                "results": {"rows": [{"id": "stale"}]},
            },
            {
                "name": "ask_erp_kpi",
                "results": {"rows": [{"product_id": "fresh"}]},
            },
        ]
        rows = _mine_ask_data_rows(tcs, skip_superseded=True)
        assert len(rows) == 1
        assert rows[0]["product_id"] == "fresh"

    def test_superseded_not_skipped_when_flag_off(self):
        tcs = [
            {
                "name": "ask_data_agent",
                "__superseded": True,
                "results": {"rows": [{"id": "stale"}]},
            },
        ]
        rows = _mine_ask_data_rows(tcs, skip_superseded=False)
        assert len(rows) == 1

    def test_empty_tool_calls_returns_empty(self):
        assert _mine_ask_data_rows([]) == []

    def test_none_tool_calls_returns_empty(self):
        assert _mine_ask_data_rows(None) == []

    def test_result_without_rows_skipped(self):
        tcs = [
            {
                "name": "ask_erp_kpi",
                "results": {"success": True},  # no rows key
            },
        ]
        rows = _mine_ask_data_rows(tcs)
        assert rows == []

    def test_nested_data_rows(self):
        """Rows nested under 'data' key."""
        tcs = [
            {
                "name": "ask_data_agent",
                "results": {"data": {"rows": [{"id": 1}]}},
            },
        ]
        rows = _mine_ask_data_rows(tcs)
        assert len(rows) == 1

    def test_non_dict_result_skipped(self):
        tcs = [
            {
                "name": "ask_data_agent",
                "results": "error string",
            },
        ]
        rows = _mine_ask_data_rows(tcs)
        assert rows == []

    def test_max_rows_cap(self):
        """_DASHBOARD_MAX_ROWS caps output at 500 rows."""
        many_rows = [{"id": i} for i in range(1000)]
        tcs = [
            {
                "name": "ask_data_agent",
                "results": {"rows": many_rows},
            },
        ]
        rows = _mine_ask_data_rows(tcs)
        assert len(rows) == 500  # capped at _DASHBOARD_MAX_ROWS


# ═══════════════════════════════════════════════════════════════════
# 2. _iter_historical_data_results — conversation history scanning
# ═══════════════════════════════════════════════════════════════════

class TestIterHistoricalDataResults:
    """_iter_historical_data_results scans ALL DATA_PRODUCING_TOOLS."""

    def test_ask_data_agent_in_assistant_tool_calls(self):
        """Regression: ask_data_agent in assistant.tool_calls."""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "name": "ask_data_agent",
                        "results": {"rows": [{"id": 1}], "sql": "SELECT 1"},
                    }
                ],
            }
        ]
        results = list(_iter_historical_data_results(messages))
        assert len(results) == 1
        tc, res = results[0]
        assert res["rows"] == [{"id": 1}]

    def test_ask_erp_kpi_in_assistant_tool_calls(self):
        """New: ask_erp_kpi in assistant.tool_calls."""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "name": "ask_erp_kpi",
                        "results": {
                            "rows": [{"product_id": "isoprene", "inventory_tons": 1234.5}],
                        },
                    }
                ],
            }
        ]
        results = list(_iter_historical_data_results(messages))
        assert len(results) == 1
        tc, res = results[0]
        assert res["rows"][0]["product_id"] == "isoprene"

    def test_mixed_tools_in_history(self):
        """Multiple data-producing tools across turns."""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"name": "ask_data_agent", "results": {"rows": [{"id": 1}]}},
                    {"name": "ask_erp_kpi", "results": {"rows": [{"product_id": "dcpd"}]}},
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {"name": "execute_query", "results": {"rows": [{"col": "x"}]}},
                ],
            },
        ]
        results = list(_iter_historical_data_results(messages))
        assert len(results) == 3

    def test_non_data_tools_skipped(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"name": "ask_perception", "results": {"answer": "text"}},
                    {"name": "ask_data_agent", "results": {"rows": [{"id": 1}]}},
                ],
            }
        ]
        results = list(_iter_historical_data_results(messages))
        assert len(results) == 1

    def test_tool_role_with_json_content(self):
        """role: 'tool' messages with JSON content."""
        messages = [
            {
                "role": "tool",
                "content": json.dumps({"rows": [{"id": 1}], "sql": "SELECT 1"}),
            }
        ]
        results = list(_iter_historical_data_results(messages))
        assert len(results) == 1

    def test_reverse_chronological_order(self):
        """Newest turns first."""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"name": "ask_data_agent", "results": {"rows": [{"id": "old"}]}},
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {"name": "ask_erp_kpi", "results": {"rows": [{"product_id": "new"}]}},
                ],
            },
        ]
        results = list(_iter_historical_data_results(messages))
        assert len(results) == 2
        _, first_res = results[0]
        assert first_res["rows"][0]["product_id"] == "new"

    def test_empty_messages_returns_empty(self):
        assert list(_iter_historical_data_results([])) == []

    def test_none_messages_returns_empty(self):
        assert list(_iter_historical_data_results(None)) == []

    def test_alias_works(self):
        """_iter_historical_ask_data_results is an alias for _iter_historical_data_results."""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"name": "ask_erp_kpi", "results": {"rows": [{"product_id": "x"}]}},
                ],
            }
        ]
        results_new = list(_iter_historical_data_results(messages))
        results_old = list(_iter_historical_ask_data_results(messages))
        assert len(results_new) == len(results_old) == 1


# ═══════════════════════════════════════════════════════════════════
# 3. _mine_historical_ask_data_result — richest result finder
# ═══════════════════════════════════════════════════════════════════

class TestMineHistoricalAskDataResult:
    """_mine_historical_ask_data_result now finds richest across ALL data tools."""

    def test_prefers_report_card_payload(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "name": "ask_data_agent",
                        "results": {"rows": [{"id": 1}], "report_card_payload": {"title": "Report"}},
                    },
                ],
            },
        ]
        result = _mine_historical_ask_data_result(messages)
        assert result is not None
        assert "report_card_payload" in result

    def test_picks_most_rows(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"name": "ask_data_agent", "results": {"rows": [{"id": 1}]}},
                    {"name": "ask_erp_kpi", "results": {"rows": [{"p": "a"}, {"p": "b"}, {"p": "c"}]}},
                ],
            },
        ]
        result = _mine_historical_ask_data_result(messages)
        assert result is not None
        assert len(result["rows"]) == 3

    def test_erp_kpi_wins_over_empty_ask_data_agent(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"name": "ask_data_agent", "results": {"answer": "no data", "rows": []}},
                    {"name": "ask_erp_kpi", "results": {"rows": [{"product_id": "isoprene"}]}},
                ],
            },
        ]
        result = _mine_historical_ask_data_result(messages)
        assert result is not None
        assert result["rows"][0]["product_id"] == "isoprene"

    def test_returns_none_when_no_data(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"name": "ask_perception", "results": {"answer": "text"}},
                ],
            },
        ]
        result = _mine_historical_ask_data_result(messages)
        assert result is None


# ═══════════════════════════════════════════════════════════════════
# 4. _mine_historical_answer_rows — row extraction from history
# ═══════════════════════════════════════════════════════════════════

class TestMineHistoricalAnswerRows:
    """_mine_historical_answer_rows now includes EDIA tool rows."""

    def test_erp_kpi_rows_included(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "name": "ask_erp_kpi",
                        "results": {
                            "rows": [
                                {"product_id": "isoprene", "inventory_tons": 1234.5},
                                {"product_id": "dcpd", "inventory_tons": 5678.0},
                            ],
                        },
                    },
                ],
            },
        ]
        rows = _mine_historical_answer_rows(messages)
        assert len(rows) == 2
        product_ids = [r["product_id"] for r in rows]
        assert "isoprene" in product_ids
        assert "dcpd" in product_ids

    def test_mixed_tools_rows_merged(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"name": "ask_data_agent", "results": {"rows": [{"id": 1}]}},
                    {"name": "ask_erp_kpi", "results": {"rows": [{"product_id": "x"}]}},
                ],
            },
        ]
        rows = _mine_historical_answer_rows(messages)
        assert len(rows) == 2

    def test_superseded_skipped(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "name": "ask_erp_kpi",
                        "__superseded": True,
                        "results": {"rows": [{"product_id": "stale"}]},
                    },
                    {
                        "name": "ask_erp_kpi",
                        "results": {"rows": [{"product_id": "fresh"}]},
                    },
                ],
            },
        ]
        rows = _mine_historical_answer_rows(messages, skip_superseded=True)
        assert len(rows) == 1
        assert rows[0]["product_id"] == "fresh"
