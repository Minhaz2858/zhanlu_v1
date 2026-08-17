"""Tests for the ask_data_agent NL2SQL fast path (Tier 2 latency work).

Covers:
1. Flag ON + single bound DB source → NLAnswerService result returned
   directly; the sub-agent loop never runs (no LLM calls).
2. Fast path returns success=False → falls back to the iterative loop.
3. Fast path raises → falls back to the iterative loop.
4. Flag OFF → NLAnswerService never touched.
5. Multiple bound sources without a preferred one → fast path skipped.
6. File-kind KB → fast path skipped.
Plus a regression test for the NLAnswerService schema-linker branch, which
used to assign the raw slice_text string to schema_info and crash
_text_to_sql / _extract_citations once the catalog flags were enabled.
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


def _db_with_kb(source_kind="database", kb_id="kb-1"):
    db = MagicMock()
    kb = MagicMock()
    kb.id = kb_id
    kb.name = "warehouse"
    kb.db_type = "mysql"
    kb.source_kind = source_kind
    kb.is_deleted = False
    db.query.return_value.filter.return_value.first.return_value = kb
    return db


def _nl_payload(**over):
    payload = {
        "success": True,
        "answer": "Top customer is ACME with 12k.",
        "rows": [{"customer": "ACME", "total": 12000}],
        "sql": "SELECT customer, SUM(amount) AS total FROM orders GROUP BY customer",
        "source_id": "kb-1",
        "source_name": "warehouse",
        "citations": ["orders.customer"],
        "iterations": 1,
    }
    payload.update(over)
    return payload


async def _prose_llm(messages, tools=None, endpoint=None):
    """Loop LLM that immediately writes prose (no tool calls)."""
    return {"content": "Loop answer.", "tool_calls": []}


class TestDataAgentFastpath(unittest.TestCase):
    def setUp(self):
        from app.config import settings
        self._old = settings.DATA_AGENT_FASTPATH_ENABLED
        settings.DATA_AGENT_FASTPATH_ENABLED = True

    def tearDown(self):
        from app.config import settings
        settings.DATA_AGENT_FASTPATH_ENABLED = self._old

    def _call(self, db, args, ctx):
        from app.services.tool_handlers import delegation_tools
        return asyncio.run(delegation_tools._ask_data_agent(args, db, "u1", context=ctx))

    def test_fastpath_success_skips_loop(self):
        from app.services.tool_handlers import delegation_tools

        with patch("app.services.db.NLAnswerService") as mock_cls, \
             patch.object(delegation_tools, "_call_llm_with_retry", new=AsyncMock()) as mock_llm:
            mock_cls.return_value.answer = AsyncMock(return_value=_nl_payload())
            result = self._call(
                _db_with_kb(),
                {"question": "top 5 customers", "data_source_id": "kb-1"},
                {"bound_kb_ids": ["kb-1"]},
            )

        self.assertTrue(result["success"])
        self.assertTrue(result.get("fastpath"))
        self.assertEqual(result["answer"], "Top customer is ACME with 12k.")
        self.assertEqual(result["rows"], [{"customer": "ACME", "total": 12000}])
        self.assertEqual(result["source_name"], "warehouse")
        mock_llm.assert_not_called()

    def test_fastpath_failure_falls_back_to_loop(self):
        from app.services.tool_handlers import delegation_tools

        with patch("app.services.db.NLAnswerService") as mock_cls, \
             patch.object(delegation_tools, "_call_llm_with_retry", new=_prose_llm):
            mock_cls.return_value.answer = AsyncMock(
                return_value={"success": False, "error": "Model did not produce SQL."}
            )
            result = self._call(
                _db_with_kb(),
                {"question": "top 5 customers", "data_source_id": "kb-1"},
                {"bound_kb_ids": ["kb-1"]},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["answer"], "Loop answer.")
        self.assertNotIn("fastpath", result)

    def test_fastpath_exception_falls_back_to_loop(self):
        from app.services.tool_handlers import delegation_tools

        with patch("app.services.db.NLAnswerService") as mock_cls, \
             patch.object(delegation_tools, "_call_llm_with_retry", new=_prose_llm):
            mock_cls.return_value.answer = AsyncMock(side_effect=RuntimeError("boom"))
            result = self._call(
                _db_with_kb(),
                {"question": "top 5 customers", "data_source_id": "kb-1"},
                {"bound_kb_ids": ["kb-1"]},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["answer"], "Loop answer.")
        self.assertNotIn("fastpath", result)

    def test_flag_off_skips_fastpath(self):
        from app.config import settings
        from app.services.tool_handlers import delegation_tools
        settings.DATA_AGENT_FASTPATH_ENABLED = False

        with patch("app.services.db.NLAnswerService") as mock_cls, \
             patch.object(delegation_tools, "_call_llm_with_retry", new=_prose_llm):
            result = self._call(
                _db_with_kb(),
                {"question": "top 5 customers", "data_source_id": "kb-1"},
                {"bound_kb_ids": ["kb-1"]},
            )

        mock_cls.assert_not_called()
        self.assertEqual(result["answer"], "Loop answer.")

    def test_multiple_bound_sources_without_preferred_skips_fastpath(self):
        from app.services.tool_handlers import delegation_tools

        with patch("app.services.db.NLAnswerService") as mock_cls, \
             patch.object(delegation_tools, "_call_llm_with_retry", new=_prose_llm):
            result = self._call(
                _db_with_kb(),
                {"question": "top 5 customers"},
                {"bound_kb_ids": ["kb-1", "kb-2"]},
            )

        mock_cls.assert_not_called()
        self.assertEqual(result["answer"], "Loop answer.")

    def test_file_kind_kb_skips_fastpath(self):
        from app.services.tool_handlers import delegation_tools

        with patch("app.services.db.NLAnswerService") as mock_cls, \
             patch.object(delegation_tools, "_call_llm_with_retry", new=_prose_llm):
            result = self._call(
                _db_with_kb(source_kind="file"),
                {"question": "summarize the doc", "data_source_id": "kb-1"},
                {"bound_kb_ids": ["kb-1"]},
            )

        mock_cls.assert_not_called()
        self.assertEqual(result["answer"], "Loop answer.")


class TestNLAnswerLinkerPath(unittest.TestCase):
    """Regression: linker slice_text (str) must not be used as schema dict."""

    def setUp(self):
        from app.config import settings
        self._old = (
            settings.SCHEMA_LINKING_ENABLED,
            settings.SEMANTIC_CATALOG_ENABLED,
        )
        settings.SCHEMA_LINKING_ENABLED = True
        settings.SEMANTIC_CATALOG_ENABLED = True

    def tearDown(self):
        from app.config import settings
        settings.SCHEMA_LINKING_ENABLED, settings.SEMANTIC_CATALOG_ENABLED = self._old

    def test_linker_slice_flows_through(self):
        from app.services.db import nl_answer_service

        linker_result = {
            "slice_text": "CREATE TABLE orders (\n  customer VARCHAR NULL\n);",
            "tables": [{
                "table_meta_id": "tm1",
                "table_name": "orders",
                "columns": [{
                    "name": "customer",
                    "data_type": "VARCHAR",
                    "is_nullable": True,
                    "is_primary_key": False,
                }],
            }],
            "join_paths": [],
        }
        chat_outputs = iter([
            "SELECT customer FROM orders",  # text_to_sql
            "There is 1 customer: ACME.",   # narrate
        ])
        seen_prompts: list = []

        async def fake_chat(messages, temperature=0.0, endpoint=None):
            seen_prompts.append(messages)
            return next(chat_outputs)

        fake_exec = {
            "rows": [{"customer": "ACME"}],
            "sql": "SELECT customer FROM orders",
            "source": {"name": "warehouse"},
        }

        with patch(
            "app.services.knowledge_graph.schema_linker.link_schema",
            new=AsyncMock(return_value=linker_result),
        ), patch.object(nl_answer_service, "_chat", new=fake_chat), \
             patch.object(nl_answer_service, "QueryService") as mock_qs:
            mock_qs.return_value.execute = MagicMock(return_value=fake_exec)
            svc = nl_answer_service.NLAnswerService(_db_with_kb())
            result = asyncio.run(svc.answer("kb-1", "list customers"))

        self.assertTrue(result["success"], msg=result.get("error"))
        self.assertEqual(result["answer"], "There is 1 customer: ACME.")
        # The SQL-gen prompt must contain the linker slice text.
        sql_gen_user_msg = seen_prompts[0][1]["content"]
        self.assertIn("CREATE TABLE orders", sql_gen_user_msg)
        self.assertIn("mysql", sql_gen_user_msg)
        # Citations computed from the adapted dict (would crash on a str).
        self.assertIn("orders.customer", result["citations"])


if __name__ == "__main__":
    unittest.main()
