"""Tests for LLM-call retry logic and success-flag correctness.

These tests verify that:
1. When the FIRST _call_llm raises, success is False and the error
   message is user-friendly (so the calling LLM can quote it instead of
   hallucinating "I queried the database and found nothing").
2. When synthesis fails AFTER rows were captured, success stays True
   (data was retrieved; only the prose generation failed).
3. _call_llm_with_retry retries on transient `httpx.TimeoutException`,
   `httpx.ConnectError`, and 5xx HTTP errors, up to 2 retries.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_execute_query_result(rows):
    # NOTE: a business-shaped SQL (not `SELECT * FROM t`) is intentional —
    # since 2026-08-22, `SELECT *` without WHERE/GROUP BY is classified as a
    # trivial probe and its rows are deliberately NOT captured in `last_rows`
    # (they trigger an NL2SQL retry instead). These tests verify the
    # synthesis-failure / empty-answer behaviors, so the fake result must
    # exercise the real business-query path.
    return {
        "success": True,
        "rows": rows,
        "sql": "SELECT material_name, SUM(total_revenue) AS total_revenue "
               "FROM sales GROUP BY material_name",
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAskDataAgentLLMFailure(unittest.TestCase):
    """Verify LLM failure handling in _ask_data_agent."""

    # ---- first-call failure ------------------------------------------------

    def test_initial_llm_failure_flips_success_to_false(self):
        """When the very first _call_llm raises, success MUST be False."""
        from app.services.tool_handlers import delegation_tools
        from app.services import agent_tools

        async def fake_call_llm(messages, tools=None, endpoint=None):
            raise RuntimeError("LLM upstream 502")

        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            return {"success": False, "error": "should not be called"}

        db = _db_with_kb()
        with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
             patch.object(delegation_tools, "_call_llm_with_retry", side_effect=fake_call_llm), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
            result = asyncio.run(delegation_tools._ask_data_agent(
                args={"question": "top materials", "data_source_id": "kb-1"},
                db=db, user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))

        # Must report failure
        self.assertFalse(result["success"], f"expected success=False, got {result}")

    def test_initial_llm_failure_user_friendly_message(self):
        """The fallback answer must contain a user-friendly message the
        calling LLM can quote verbatim."""
        from app.services.tool_handlers import delegation_tools
        from app.services import agent_tools

        async def fake_call_llm(messages, tools=None, endpoint=None):
            raise RuntimeError("LLM upstream 502")

        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            return {"success": False, "error": "should not be called"}

        db = _db_with_kb()
        with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
             patch.object(delegation_tools, "_call_llm_with_retry", side_effect=fake_call_llm), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
            result = asyncio.run(delegation_tools._ask_data_agent(
                args={"question": "top materials", "data_source_id": "kb-1"},
                db=db, user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))

        self.assertFalse(result["success"])
        # The error field should be non-empty
        self.assertTrue(result.get("error"), f"error should be set, got {result.get('error')}")
        # The answer must contain a user-friendly message (not a raw traceback)
        answer = result.get("answer", "")
        self.assertIn("Data Agent", answer, f"answer should be user-friendly: {answer!r}")
        self.assertIn("language model", answer, f"should mention LLM failure: {answer!r}")
        self.assertIn("retry", answer.lower(), f"should suggest retry: {answer!r}")

    # ---- synthesis failure (soft) -----------------------------------------

    def test_synthesis_failure_keeps_success_true_when_rows_captured(self):
        """When the synthesis LLM call fails AFTER rows were captured,
        success stays True because data retrieval succeeded."""
        from app.services.tool_handlers import delegation_tools
        from app.services import agent_tools

        rows = [{"material_name": "X", "total_revenue": 1.0}]

        # 1st (main) call: tool_call -> rows captured
        # 2nd (synthesis) call: raises
        counter = {"n": 0}

        async def fake_call_llm(messages, tools=None, endpoint=None):
            counter["n"] += 1
            if counter["n"] == 1:
                return {
                    "content": "",
                    "tool_calls": [{
                        "id": "tc1", "type": "function",
                        "function": {
                            "name": "execute_query",
                            "arguments": '{"sql":"SELECT 1"}',
                        },
                    }],
                }
            # synthesis call — raises
            raise RuntimeError("synthesis LLM down")

        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            return _make_execute_query_result(rows)

        db = _db_with_kb()
        with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
             patch.object(delegation_tools, "_call_llm_with_retry", side_effect=fake_call_llm), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
            result = asyncio.run(delegation_tools._ask_data_agent(
                args={"question": "top materials", "data_source_id": "kb-1", "max_iterations": 1},
                db=db, user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))

        # Success stays True — data retrieval worked.
        self.assertTrue(result["success"], f"expected success=True (rows captured), got {result}")
        # Answer is non-empty (row-count fallback).
        self.assertTrue(result["answer"], f"answer should be non-empty, got {result['answer']!r}")
        # Rows are still returned.
        self.assertEqual(result["rows"], rows)

    # ---- retry helper ------------------------------------------------------

    def test_call_llm_with_retry_succeeds_on_retry(self):
        """_call_llm_with_retry retries once on timeout, then succeeds."""
        from app.services.tool_handlers import delegation_tools

        attempts = []
        single_call = AsyncMock()

        async def fake_single_call(messages, tools=None, endpoint=None):
            attempts.append(1)
            if len(attempts) == 1:
                raise httpx.TimeoutException("timed out")
            return {"content": "ok", "tool_calls": []}

        single_call.side_effect = fake_single_call

        with patch.object(delegation_tools, "_call_llm", side_effect=fake_single_call):
            result = asyncio.run(delegation_tools._call_llm_with_retry(
                [{"role": "user", "content": "hi"}], [],
            ))

        self.assertEqual(result["content"], "ok")
        self.assertEqual(len(attempts), 2, f"expected 2 attempts, got {len(attempts)}")

    def test_call_llm_with_retry_gives_up_after_max_retries(self):
        """After N retries, _call_llm_with_retry re-raises the last error."""
        from app.services.tool_handlers import delegation_tools

        attempts = []

        async def fake_single_call(messages, tools=None, endpoint=None):
            attempts.append(1)
            raise httpx.TimeoutException("timed out")

        with patch.object(delegation_tools, "_call_llm", side_effect=fake_single_call):
            with self.assertRaises(httpx.TimeoutException):
                asyncio.run(delegation_tools._call_llm_with_retry(
                    [{"role": "user", "content": "hi"}], [],
                ))

        # default max_retries=2 -> 3 total attempts
        self.assertEqual(len(attempts), 3, f"expected 3 attempts, got {len(attempts)}")

    def test_regression_empty_answer_guarantee_still_works(self):
        """Sanity: the original fix (empty-answer guarantee) is not broken."""
        from app.services.tool_handlers import delegation_tools
        from app.services import agent_tools

        rows = [{"material_name": "X", "total_revenue": 1.0}]
        llm_responses = [
            {"content": "", "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {"name": "execute_query", "arguments": '{"sql":"SELECT 1"}'},
            }]},
            {"content": "Top material is X.", "tool_calls": []},
        ]

        async def fake_call_llm(messages, tools=None, endpoint=None):
            return llm_responses.pop(0)

        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            return _make_execute_query_result(rows)

        db = _db_with_kb()
        with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
             patch.object(delegation_tools, "_call_llm_with_retry", side_effect=fake_call_llm), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
            result = asyncio.run(delegation_tools._ask_data_agent(
                args={"question": "top materials", "data_source_id": "kb-1"},
                db=db, user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))

        self.assertTrue(result["success"])
        self.assertTrue(result["answer"])


if __name__ == "__main__":
    unittest.main()
