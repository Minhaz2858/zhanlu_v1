"""Tests for the data-agent empty-answer guarantee (task 2).

The data-agent sub-loop in delegation_tools.py is supposed to ALWAYS
return a non-empty `answer` field, even if the LLM tries to stop with
tool_calls and an empty `content`.  This test mocks the LLM and
`execute_tool` and asserts that:

1. If the LLM never writes prose but the rows were captured, the
   sub-loop forces one final synthesis call and the final answer is
   non-empty.
2. If the LLM writes prose on its own, the synthesis fallback is
   skipped (we don't pay the extra LLM call cost).
3. If the LLM raises on the synthesis fallback, we still return a
   non-empty fallback string (so the calling agent never sees None).
4. If no rows are ever captured, we still return a non-empty answer.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _make_execute_query_result(rows):
    """Mimic the dict execute_query returns."""
    return {
        "success": True,
        "rows": rows,
        "sql": "SELECT * FROM t",
        "source": {"id": "kb-1", "name": "db_zhanlu_no1"},
    }


def _db_with_kb():
    db = MagicMock()
    kb = MagicMock()
    kb.id = "kb-1"
    kb.name = "db_zhanlu_no1"
    kb.is_deleted = False
    db.query.return_value.filter.return_value.first.return_value = kb
    return db


class TestDataAgentEmptyAnswerFix(unittest.TestCase):
    """Verify the empty-answer guarantee in _ask_data_agent."""

    def _run(self, llm_responses, *, rows_to_capture=None, capture_rows=True,
             max_iterations=None):
        """Run _ask_data_agent with a sequence of mocked LLM responses.

        If capture_rows is True, mocked execute_tool will return
        rows_to_capture.  Otherwise mocked execute_tool will return an
        empty result.
        """
        from app.services.tool_handlers import delegation_tools
        from app.services import agent_tools

        call_log: list[dict] = []

        async def fake_call_llm(messages, tools=None, endpoint=None):
            call_log.append({"n": len(call_log), "tools": tools, "messages": list(messages)})
            if not llm_responses:
                raise RuntimeError("no more LLM responses mocked")
            return llm_responses.pop(0)

        # Mock the execute_tool import inside the sub-loop
        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            if not capture_rows:
                return {"success": False, "error": "no rows captured"}
            return _make_execute_query_result(rows_to_capture or [])

        db = _db_with_kb()
        args = {"question": "top materials by revenue", "data_source_id": "kb-1"}
        if max_iterations is not None:
            args["max_iterations"] = max_iterations
        with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
            result = asyncio.run(delegation_tools._ask_data_agent(
                args=args,
                db=db,
                user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))
            return result, call_log

    def test_synthesis_fires_when_llm_returns_empty_prose_with_tool_calls(self):
        """The LLM ran execute_query but never wrote prose. We force a
        synthesis turn. The synthesis reply becomes the final answer.

        To force the synthesis branch, the for-loop must exit without
        final_text being set.  We hit max_iterations=2 with two
        tool-call responses, so the for-loop's `else` branch fires and
        final_text is set to the placeholder; then the empty-answer
        check fires and we run the synthesis LLM call.
        """
        rows = [{"material_name": "X", "total_revenue": 1.0}]

        llm_responses = [
            # 1st turn: LLM calls execute_query, content=""
            {"content": "", "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {"name": "execute_query", "arguments": '{"sql":"SELECT 1"}'},
            }]},
            # 2nd turn: LLM calls execute_query AGAIN (waste), content=""
            {"content": "", "tool_calls": [{
                "id": "tc2", "type": "function",
                "function": {"name": "execute_query", "arguments": '{"sql":"SELECT 2"}'},
            }]},
            # (synthesis fallback): LLM writes prose, no tools
            {"content": "Top material is X with revenue 1.0.", "tool_calls": []},
        ]

        result, calls = self._run(llm_responses, rows_to_capture=rows, max_iterations=2)

        # 2 calls in the for-loop, plus 1 synthesis call = 3 total.
        self.assertEqual(len(calls), 3, f"expected 3 LLM calls, got {len(calls)}")
        # The synthesis call (3rd) was made with NO tools (empty list).
        self.assertEqual(calls[2]["tools"], [], f"synthesis turn must have empty tools, got {calls[2]['tools']}")
        # Final answer is non-empty and matches the LLM's prose.
        self.assertTrue(result["answer"], "answer should be non-empty")
        self.assertIn("Top material", result["answer"])
        # Rows are still returned.
        self.assertEqual(result["rows"], rows)
        self.assertTrue(result["success"])

    def test_synthesis_skipped_when_llm_writes_prose_directly(self):
        """If the LLM wrote prose on its own, we don't pay the extra call."""
        rows = [{"material_name": "X", "total_revenue": 1.0}]

        llm_responses = [
            {"content": "", "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {"name": "execute_query", "arguments": '{"sql":"SELECT 1"}'},
            }]},
            # LLM writes prose — no more tool calls.
            {"content": "Here are the top materials: X leads at 1.0.", "tool_calls": []},
        ]

        result, calls = self._run(llm_responses, rows_to_capture=rows)

        # Only 2 LLM calls (no synthesis fallback).
        self.assertEqual(len(calls), 2, f"expected 2 LLM calls, got {len(calls)}")
        # The LLM's prose is the final answer.
        self.assertIn("X leads at 1.0", result["answer"])
        self.assertTrue(result["success"])

    def test_fallback_string_when_synthesis_also_fails(self):
        """If the synthesis LLM call raises, we still return non-empty prose."""
        from app.services.tool_handlers import delegation_tools
        from app.services import agent_tools

        rows = [{"material_name": "X", "total_revenue": 1.0}]

        # 1st call: tool_call (captures rows). 2nd call: synthesis, raises.
        call_log: list[dict] = []
        responses = [
            {"content": "", "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {"name": "execute_query", "arguments": '{"sql":"SELECT 1"}'},
            }]},
            RuntimeError("synthesis LLM down"),
        ]

        async def fake_call_llm(messages, tools=None, endpoint=None):
            call_log.append({"n": len(call_log), "tools": tools})
            nxt = responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            return _make_execute_query_result(rows)

        db = _db_with_kb()
        with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
            result = asyncio.run(delegation_tools._ask_data_agent(
                args={"question": "top materials", "data_source_id": "kb-1", "max_iterations": 1},
                db=db, user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))

        # Even though the synthesis call failed, answer is non-empty.
        self.assertTrue(result["answer"], f"answer should be non-empty fallback, got {result['answer']!r}")
        # The fallback mentions row count.
        self.assertIn("1 row", result["answer"])
        # We still have the rows.
        self.assertEqual(result["rows"], rows)

    def test_fallback_when_no_rows_at_all(self):
        """If the LLM never got rows, the placeholder says so explicitly."""
        # LLM only calls list_data_sources (no rows captured), then writes
        # empty prose.  We still want a non-empty answer.
        llm_responses = [
            {"content": "", "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {"name": "list_data_sources", "arguments": "{}"},
            }]},
            # LLM doesn't write prose.  We force a synthesis turn; with
            # no rows, it still returns a final answer (placeholder).
            {"content": "", "tool_calls": []},
        ]
        result, calls = self._run(llm_responses, capture_rows=False)
        self.assertTrue(result["answer"], f"answer should be non-empty, got {result['answer']!r}")
        self.assertIsNone(result["rows"])


if __name__ == "__main__":
    unittest.main()
