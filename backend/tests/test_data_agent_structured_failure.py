"""Tests for the data-agent structured failure on metadata-only results.

Fix 1 of the goal-contract pipeline: when the sub-agent's last query returns
only date-range / row-count aggregates (MIN/MAX/COUNT columns, no business
dimensions), `_ask_data_agent` must return a STRUCTURED failure
(success: False, error_kind: "metadata_only") instead of a "success with
metadata rows" that the calling agent would render as real data.  The rows
still pass through so the main loop's GoalContract can count the
metadata-only shape and force a real query on exit.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.tool_handlers import delegation_tools


class TestDataAgentStructuredFailure(unittest.TestCase):
    """The honesty gate on the sub-agent return shape."""

    def _run(self, llm_responses, rows_to_capture=None, question=None):
        from app.services import agent_tools

        async def fake_call_llm(messages, tools=None, endpoint=None):
            if not llm_responses:
                raise RuntimeError("no more LLM responses mocked")
            return llm_responses.pop(0)

        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            sql = args.get("sql", "")
            return {
                "success": True,
                "rows": rows_to_capture or [],
                "sql": sql,
                "source": {"id": "kb-1", "name": "db_zhanlu_no1"},
            }

        # The sub-loop self-eval gate (SELF_EVAL_REPLAN_ENABLED=true in .env)
        # runs its own LLM/verification path which would otherwise consume the
        # mocked responses or hit the network. Disable it so these tests
        # exercise ONLY the honesty gate in isolation.
        async def fake_gate(*args, **kwargs):
            return "", ""

        db = MagicMock()
        kb = MagicMock()
        kb.id = "kb-1"
        kb.name = "db_zhanlu_no1"
        kb.is_deleted = False
        db.query.return_value.filter.return_value.first.return_value = kb

        args = {
            "question": question or "what is the date range of sales?",
            "data_source_id": "kb-1",
        }
        with patch.object(delegation_tools, "_sub_loop_answer_gate", side_effect=fake_gate), \
             patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
            return asyncio.run(delegation_tools._ask_data_agent(
                args=args, db=db, user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))

    def test_metadata_only_rows_return_structured_failure(self):
        """MIN/MAX-only rows → success False + error_kind metadata_only."""
        rows = [{"MIN_FDATE": "2026-01-01", "MAX_FDATE": "2026-08-01"}]
        llm_responses = [
            {"content": "", "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {
                    "name": "execute_query",
                    "arguments": '{"sql":"SELECT MIN(FDATE), MAX(FDATE) FROM sales"}',
                },
            }]},
            {"content": "The date range is 2026-01-01 to 2026-08-01.", "tool_calls": []},
        ]
        result = self._run(llm_responses, rows_to_capture=rows)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_kind"], "metadata_only")
        self.assertIn("metadata", result["error"])
        # The honest context overrides any LLM prose about the metadata rows.
        self.assertIn("metadata", result["answer"])
        # Rows still pass through so the contract can count the shape.
        self.assertEqual(result["rows"], rows)

    def test_count_only_rows_also_flagged(self):
        """COUNT-only rows → structured failure too."""
        rows = [{"count_rows": 12}]
        llm_responses = [
            {"content": "", "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {
                    "name": "execute_query",
                    "arguments": '{"sql":"SELECT COUNT(*) AS count_rows FROM orders"}',
                },
            }]},
            {"content": "There are 12 orders.", "tool_calls": []},
        ]
        result = self._run(llm_responses, rows_to_capture=rows)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_kind"], "metadata_only")

    def test_function_form_aggregates_flagged(self):
        """min(...)/max(...) function-form columns are metadata-only too."""
        rows = [{"min(fdate)": "2026-01-01", "max(fdate)": "2026-08-01"}]
        llm_responses = [
            {"content": "", "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {
                    "name": "execute_query",
                    "arguments": '{"sql":"SELECT min(fdate), max(fdate) FROM sales"}',
                },
            }]},
            {"content": "Range is 2026-01-01 to 2026-08-01.", "tool_calls": []},
        ]
        result = self._run(llm_responses, rows_to_capture=rows)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_kind"], "metadata_only")

    def test_business_rows_not_flagged(self):
        """Dimension + measure rows must NOT be flagged."""
        rows = [{"product_name": "Widget", "total_revenue": 100.0}]
        llm_responses = [
            {"content": "", "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {
                    "name": "execute_query",
                    "arguments": (
                        '{"sql":"SELECT product_name, SUM(price) AS total_revenue '
                        'FROM order_items GROUP BY product_name"}'
                    ),
                },
            }]},
            {"content": "Widget: 100.0", "tool_calls": []},
        ]
        result = self._run(llm_responses, rows_to_capture=rows)

        self.assertTrue(result["success"])
        self.assertIsNone(result.get("error_kind"))
        self.assertEqual(result["rows"], rows)


if __name__ == "__main__":
    unittest.main()
