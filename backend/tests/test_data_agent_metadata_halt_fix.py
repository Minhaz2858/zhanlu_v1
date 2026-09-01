"""Tests for the data-agent metadata-halt fix (metadata query detection).

The data-agent sub-loop was halting after a single `information_schema.tables`
query, capturing the metadata rows as "last_rows", and then synthesizing a
useless report about table metadata.  These tests verify that:

1. `_is_metadata_query` correctly identifies catalog/schema queries.
2. `_maybe_capture_execute_result` stores metadata rows separately.
3. Metadata-only results do NOT poison `last_rows`.
4. The honest metadata-only fallback message is returned when no business data
   was fetched.
5. Normal business queries continue to work.
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

from app.services.tool_handlers import delegation_tools


class TestIsMetadataQuery(unittest.TestCase):
    """Unit tests for _is_metadata_query."""

    def _check(self, sql, expected):
        self.assertEqual(
            delegation_tools._is_metadata_query(sql),
            expected,
            f"_is_metadata_query({sql!r}) expected {expected}",
        )

    def test_information_schema(self):
        self._check("SELECT * FROM information_schema.tables", True)
        self._check("select table_name from INFORMATION_SCHEMA.TABLES", True)

    def test_pg_catalog(self):
        self._check("SELECT * FROM pg_catalog.pg_tables", True)
        self._check("SELECT * FROM pg_class", True)
        self._check("SELECT * FROM pg_namespace", True)

    def test_show_tables(self):
        self._check("SHOW TABLES", True)
        self._check("show tables", True)
        self._check("SHOW FULL TABLES", True)

    def test_show_columns(self):
        self._check("SHOW COLUMNS FROM t", True)
        self._check("show full columns from t", True)

    def test_describe(self):
        self._check("DESCRIBE t", True)
        self._check("describe t", True)
        self._check("DESC t", True)

    def test_explain(self):
        self._check("EXPLAIN SELECT * FROM t", True)
        self._check("explain SELECT * FROM t", True)

    def test_mysql_sys_schemas(self):
        self._check("SELECT * FROM mysql.user", True)
        self._check("SELECT * FROM sys.processlist", True)

    def test_business_queries_not_metadata(self):
        self._check("SELECT * FROM t", False)
        self._check("SELECT * FROM information_schema_mock", False)
        self._check("SELECT * FROM sales", False)
        self._check("SELECT * FROM actual_price", False)
        self._check("INSERT INTO t VALUES (1)", False)
        self._check("UPDATE t SET x = 1", False)
        self._check(None, False)
        self._check("", False)


class TestMaybeCaptureExecuteResult(unittest.TestCase):
    """Unit tests for _maybe_capture_execute_result metadata separation."""

    def _make_result(self, sql, rows, success=True):
        return {
            "success": success,
            "rows": rows,
            "sql": sql,
            "source": {"id": "kb-1", "name": "db_zhanlu_no1"},
        }

    def test_metadata_query_stored_separately(self):
        state = {}
        delegation_tools._maybe_capture_execute_result(
            "execute_query",
            self._make_result("SELECT table_name FROM information_schema.tables", ["r1"]),
            state,
        )
        self.assertIsNone(state.get("last_rows"))
        self.assertEqual(state.get("last_metadata_rows"), ["r1"])
        self.assertEqual(state.get("last_metadata_sql"), "SELECT table_name FROM information_schema.tables")
        self.assertEqual(state.get("source_id"), "kb-1")
        self.assertEqual(state.get("source_name"), "db_zhanlu_no1")

    def test_business_query_stored_normally(self):
        state = {}
        delegation_tools._maybe_capture_execute_result(
            "execute_query",
            self._make_result("SELECT * FROM sales", [{"id": 1}]),
            state,
        )
        self.assertEqual(state.get("last_rows"), [{"id": 1}])
        self.assertIsNone(state.get("last_metadata_rows"))
        self.assertEqual(state.get("last_sql"), "SELECT * FROM sales")
        self.assertEqual(state.get("source_id"), "kb-1")

    def test_answer_from_database_metadata_separation(self):
        state = {}
        res = {
            "success": True,
            "rows": ["meta1"],
            "sql": "SHOW TABLES",
            "source_id": "kb-1",
            "source_name": "db_zhanlu_no1",
        }
        delegation_tools._maybe_capture_execute_result(
            "answer_from_database", res, state
        )
        self.assertIsNone(state.get("last_rows"))
        self.assertEqual(state.get("last_metadata_rows"), ["meta1"])
        self.assertEqual(state.get("last_metadata_sql"), "SHOW TABLES")

    def test_failed_result_no_state_change(self):
        state = {}
        delegation_tools._maybe_capture_execute_result(
            "execute_query",
            self._make_result("SELECT * FROM sales", [{"id": 1}], success=False),
            state,
        )
        self.assertEqual(state, {})

    def test_metadata_overwrite_and_business_later(self):
        """A metadata query followed by a business query should replace metadata."""
        state = {}
        delegation_tools._maybe_capture_execute_result(
            "execute_query",
            self._make_result("SHOW TABLES", ["t1"]),
            state,
        )
        delegation_tools._maybe_capture_execute_result(
            "execute_query",
            self._make_result("SELECT * FROM sales", [{"id": 1}]),
            state,
        )
        self.assertEqual(state.get("last_rows"), [{"id": 1}])
        self.assertEqual(state.get("last_metadata_rows"), ["t1"])
        self.assertEqual(state.get("last_sql"), "SELECT * FROM sales")
        self.assertEqual(state.get("last_metadata_sql"), "SHOW TABLES")


class TestMetadataHaltSubLoop(unittest.TestCase):
    """Integration tests for the full _ask_data_agent sub-loop with metadata."""

    def _run(self, llm_responses, *, rows_to_capture=None, capture_rows=True,
             max_iterations=None, metadata_rows=None):
        """Run _ask_data_agent with mocked LLM and execute_tool."""
        from app.services import agent_tools

        call_log: list[dict] = []

        async def fake_call_llm(messages, tools=None, endpoint=None):
            call_log.append({"n": len(call_log), "tools": tools, "messages": list(messages)})
            if not llm_responses:
                raise RuntimeError("no more LLM responses mocked")
            return llm_responses.pop(0)

        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            if not capture_rows:
                return {"success": False, "error": "no rows captured"}
            # Simulate metadata vs business based on SQL
            sql = args.get("sql", "")
            if delegation_tools._is_metadata_query(sql):
                return {
                    "success": True,
                    "rows": metadata_rows or [],
                    "sql": sql,
                    "source": {"id": "kb-1", "name": "db_zhanlu_no1"},
                }
            return {
                "success": True,
                "rows": rows_to_capture or [],
                "sql": sql,
                "source": {"id": "kb-1", "name": "db_zhanlu_no1"},
            }

        db = MagicMock()
        kb = MagicMock()
        kb.id = "kb-1"
        kb.name = "db_zhanlu_no1"
        kb.is_deleted = False
        db.query.return_value.filter.return_value.first.return_value = kb

        args = {"question": "top materials by revenue", "data_source_id": "kb-1"}
        if max_iterations is not None:
            args["max_iterations"] = max_iterations

        # The sub-loop self-eval gate (SELF_EVAL_REPLAN_ENABLED=true in .env)
        # runs its own verification path which appends a gap disclosure to the
        # answer — that would pollute these assertions. Disable it so the tests
        # exercise ONLY the metadata-halt behavior deterministically.
        async def fake_gate(*args, **kwargs):
            return "", ""

        with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
             patch.object(delegation_tools, "_sub_loop_answer_gate", side_effect=fake_gate), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
            result = asyncio.run(delegation_tools._ask_data_agent(
                args=args,
                db=db,
                user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))
            return result, call_log

    def test_metadata_only_no_synthesis(self):
        """LLM queries information_schema and stops. last_rows stays None,
        so synthesis fallback is skipped, and we get the honest metadata-only
        placeholder message."""
        metadata_rows = [{"table_name": "t1"}, {"table_name": "t2"}]

        llm_responses = [
            # 1st turn: LLM calls execute_query with metadata SQL
            {"content": "", "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {
                    "name": "execute_query",
                    "arguments": '{"sql":"SELECT table_name FROM information_schema.tables"}',
                },
            }]},
            # 2nd turn: LLM tries to write prose (but the prompt says it shouldn't
            # stop after metadata).  We simulate it stopping anyway to test the
            # safety net.
            {"content": "", "tool_calls": []},
        ]

        result, calls = self._run(
            llm_responses,
            metadata_rows=metadata_rows,
            max_iterations=2,
        )

        # No synthesis call should happen because last_rows is None.
        self.assertEqual(len(calls), 2, f"expected 2 LLM calls, got {len(calls)}")
        # The honest metadata-only fallback message should be returned.
        self.assertTrue(result["answer"], "answer should be non-empty")
        self.assertIn("discovered table schemas", result["answer"])
        self.assertIn("did not fetch any business data", result["answer"])
        # Rows should be empty (no business data retrieved).
        self.assertIsNone(result.get("rows"))
        self.assertTrue(result["success"])

    def test_business_query_after_metadata(self):
        """LLM first does metadata discovery, then runs a real query."""
        business_rows = [{"material_name": "X", "total_revenue": 1.0}]
        metadata_rows = [{"table_name": "sales"}]

        llm_responses = [
            # 1st turn: metadata discovery
            {"content": "", "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {
                    "name": "execute_query",
                    "arguments": '{"sql":"SELECT table_name FROM information_schema.tables"}',
                },
            }]},
            # 2nd turn: real business query
            {"content": "", "tool_calls": [{
                "id": "tc2", "type": "function",
                "function": {
                    "name": "execute_query",
                    "arguments": '{"sql":"SELECT * FROM sales"}',
                },
            }]},
            # 3rd turn: LLM writes prose
            {"content": "Top material is X with revenue 1.0.", "tool_calls": []},
        ]

        result, calls = self._run(
            llm_responses,
            rows_to_capture=business_rows,
            metadata_rows=metadata_rows,
        )

        # 3 calls total (2 tool turns + 1 prose turn).
        self.assertEqual(len(calls), 3)
        self.assertIn("Top material", result["answer"])
        self.assertEqual(result["rows"], business_rows)
        self.assertTrue(result["success"])

    def test_existing_empty_answer_regression(self):
        """Ensure the existing empty-answer guarantee still works for normal
        business queries that use 'SELECT * FROM t' (non-metadata SQL)."""
        rows = [{"id": 1}]

        llm_responses = [
            {"content": "", "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {"name": "execute_query", "arguments": '{"sql":"SELECT * FROM t"}'},
            }]},
            {"content": "", "tool_calls": [{
                "id": "tc2", "type": "function",
                "function": {"name": "execute_query", "arguments": '{"sql":"SELECT * FROM t"}'},
            }]},
            {"content": "Found one row.", "tool_calls": []},
        ]

        result, calls = self._run(
            llm_responses,
            rows_to_capture=rows,
            max_iterations=2,
        )

        self.assertEqual(len(calls), 3)
        self.assertEqual(result["rows"], rows)
        self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
