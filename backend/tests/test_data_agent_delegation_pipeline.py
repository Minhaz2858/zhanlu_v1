"""Acceptance tests for the data-agent delegation pipeline fixes (2026-08-22).

Covers the five fixes from the Ecisco BI latency + empty-answer investigation:

(a) Caller-visible payload includes condensed rows — when the sub-agent has
    rows but no prose, the `answer` field carries a "DATA READY" directive
    WITH the first rows inline (so the calling model always has real numbers).
(b) Soft-failure + rows -> apology-guard forces re-synthesis — the generic
    "I had trouble putting it all together" apology (EN + zh) is recognized
    and, when rows exist, swapped for a data-aware message (never surfaced).
(c) Schema slice in the question -> zero describe_schema calls — the
    `[schema: ...]` block is injected into the sub-agent prompt so it can
    skip discovery; verified at the builder level and via the tool log.
(d) Wall-clock cap — a slow sub-model cannot hold the turn hostage; on
    timeout the delegation returns captured rows with `truncated: True`.
(e) max_iterations respected — default 2, hard cap 6.
"""

import asyncio
import os
import sys
import time
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


def _tool_call(tc_id, name="execute_query", args=None):
    return {
        "id": tc_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": args or '{"sql":"SELECT 1"}',
        },
    }


class TestCondensedRowSummary(unittest.TestCase):
    """(a) The condensed row text the caller actually sees."""

    def test_condensed_rows_skip_ids_and_include_values(self):
        from app.services.tool_handlers.delegation_tools import _condensed_row_text

        rows = [
            {"FID": 1, "FMATERIALID": 103350, "product": "Widget", "revenue": 100.0},
            {"FID": 2, "FMATERIALID": 103352, "product": "Gadget", "revenue": 200.0},
        ]
        out = _condensed_row_text(rows)
        # FK/id columns are skipped, business values are present.
        self.assertNotIn("FID=", out)
        self.assertIn("product=Widget", out)
        self.assertIn("revenue=100.0", out)

    def test_condensed_rows_capped_at_max_rows_and_chars(self):
        from app.services.tool_handlers.delegation_tools import _condensed_row_text

        rows = [{"k": i, "v": "x" * 50} for i in range(20)]
        out = _condensed_row_text(rows, max_rows=3, max_chars=120)
        self.assertLessEqual(len(out), 123, f"condensed text too long: {len(out)}")
        self.assertNotIn("k=3", out, "rows beyond max_rows must be excluded")

    def test_condensed_empty(self):
        from app.services.tool_handlers.delegation_tools import _condensed_row_text

        self.assertEqual(_condensed_row_text([]), "(no rows)")


class TestDirectiveFallbackWithRows(unittest.TestCase):
    """(a) When the sub-agent has rows but no prose, `answer` is a directive
    containing the condensed row values — not a passive "Retrieved N rows"."""

    def _run(self, llm_responses, rows, max_iterations=2):
        from app.services.tool_handlers import delegation_tools
        from app.services import agent_tools

        async def fake_call_llm(messages, tools=None, endpoint=None):
            if not llm_responses:
                raise RuntimeError("no more LLM responses mocked")
            return llm_responses.pop(0)

        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            return _make_execute_query_result(rows)

        async def fake_gate(*args, **kwargs):
            return "", ""

        db = _db_with_kb()
        with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
             patch.object(delegation_tools, "_sub_loop_answer_gate", side_effect=fake_gate), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
            return asyncio.run(delegation_tools._ask_data_agent(
                args={"question": "top materials", "data_source_id": "kb-1",
                      "max_iterations": max_iterations},
                db=db, user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))

    def test_rows_without_prose_yield_data_ready_directive(self):
        rows = [{"material_name": "X", "total_revenue": 1.0}]
        llm_responses = [
            # iter 1: call execute_query (captures rows)
            {"content": "", "tool_calls": [_tool_call("tc1")]},
            # iter 2: no tools, no prose
            {"content": "", "tool_calls": []},
            # synthesis turn: still no prose -> directive fallback fires
            {"content": "", "tool_calls": []},
        ]
        result = self._run(llm_responses, rows)

        self.assertTrue(result["success"])
        self.assertIn("DATA READY", result["answer"])
        # The actual business value is visible in the answer field.
        self.assertIn("material_name=X", result["answer"])
        self.assertIn("total_revenue=1.0", result["answer"])
        # Rows still pass through unchanged.
        self.assertEqual(result["rows"], rows)

    def test_rows_without_prose_and_synthesis_failure(self):
        """Even when the synthesis LLM call raises, the directive fallback
        still carries the condensed rows."""
        from app.services.tool_handlers import delegation_tools
        from app.services import agent_tools

        rows = [{"product": "Widget", "qty": 42.0}]

        async def fake_call_llm(messages, tools=None, endpoint=None):
            if len([m for m in messages if m.get("role") == "assistant"]) > 0:
                # treat any follow-up as synthesis -> hard failure
                raise RuntimeError("synthesis LLM down")
            return {"content": "", "tool_calls": [_tool_call("tc1")]}

        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            return _make_execute_query_result(rows)

        async def fake_gate(*args, **kwargs):
            return "", ""

        db = _db_with_kb()
        with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
             patch.object(delegation_tools, "_sub_loop_answer_gate", side_effect=fake_gate), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
            result = asyncio.run(delegation_tools._ask_data_agent(
                args={"question": "qty by product", "data_source_id": "kb-1",
                      "max_iterations": 1},
                db=db, user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))

        self.assertTrue(result["success"])
        self.assertIn("DATA READY", result["answer"])
        self.assertIn("product=Widget", result["answer"])
        self.assertEqual(result["rows"], rows)


class TestApologyGuard(unittest.TestCase):
    """(b) The apology pattern + data-aware fallback in agents.py."""

    def _import(self):
        from app.routers import agents
        return agents

    def test_pattern_matches_english_apology(self):
        agents = self._import()
        apology = (
            "I gathered some information but had trouble putting it all "
            "together. Could you try again with a more specific request?"
        )
        self.assertTrue(agents._APOLOGY_PATTERN_RE.search(apology))
        self.assertTrue(agents._APOLOGY_PATTERN_RE.search(
            "I was unable to synthesize the results into a clear answer."
        ))

    def test_pattern_matches_chinese_apology(self):
        agents = self._import()
        self.assertTrue(agents._APOLOGY_PATTERN_RE.search(
            "我收集了一些信息，但无法将结果整合在一起。"
        ))
        self.assertTrue(agents._APOLOGY_PATTERN_RE.search(
            "我尝试进行汇总，但没有给出完整答案。"
        ))

    def test_pattern_ignores_real_answer(self):
        agents = self._import()
        real = (
            "July 2026 sales totaled ¥12.4M across 41 products, up 6.2% "
            "vs June. The top product was Widget at ¥3.1M."
        )
        self.assertIsNone(agents._APOLOGY_PATTERN_RE.search(real))

    def test_has_data_rows(self):
        agents = self._import()
        self.assertTrue(agents._has_data_rows([
            {"name": "ask_data_agent", "results": {"rows": [{"a": 1}]}},
        ]))
        self.assertTrue(agents._has_data_rows([
            {"name": "execute_query", "results": {"rows": [{"a": 1}]}},
        ]))
        self.assertFalse(agents._has_data_rows([
            {"name": "ask_data_agent", "results": {"rows": []}},
        ]))
        self.assertFalse(agents._has_data_rows([
            {"name": "ask_data_agent", "results": {}},
        ]))
        self.assertFalse(agents._has_data_rows([
            {"name": "search_files", "results": {"rows": [{"a": 1}]}},
        ]))

    def test_data_rows_fallback_is_not_an_apology(self):
        agents = self._import()
        msg = agents._data_rows_fallback([
            {"name": "ask_data_agent", "results": {
                "rows": [{"product": "Widget", "revenue": 100.0}],
                "source_name": "erp",
            }},
        ])
        self.assertIn("1 rows", msg)
        self.assertIn("erp", msg)
        self.assertNotIn("had trouble", msg.lower())

    def test_choose_fallback_prefers_data_when_rows_exist(self):
        agents = self._import()
        fallback = agents._choose_fallback(
            [
                {"name": "ask_data_agent", "results": {
                    "rows": [{"product": "Widget", "revenue": 100.0}],
                    "source_name": "erp",
                }},
            ],
            [],
            user_content="sales report",
        )
        # Must NOT be the generic apology.
        self.assertNotIn("had trouble", fallback.lower())
        self.assertTrue(fallback.strip())


class TestSchemaSlice(unittest.TestCase):
    """(c) The [schema: ...] slice builder + prompt injection."""

    def test_build_schema_slice_from_catalog(self):
        from app.models.knowledge_catalog import (
            KBColumnMeta, KBTableMeta, KBTableRelation,
        )
        from app.services.data_source_runtime.data_source_runtime import (
            _build_schema_slice,
        )

        t_header = MagicMock()
        t_header.id = "t1"; t_header.kb_id = "kb-1"
        t_header.table_name = "erp_t_sal_outstock"; t_header.table_type = "TABLE"
        t_header.table_role = "dimension"
        t_line = MagicMock()
        t_line.id = "t2"; t_line.kb_id = "kb-1"
        t_line.table_name = "erp_t_sal_outstockentry"; t_line.table_type = "TABLE"
        t_line.table_role = "fact"

        c_h_fid = MagicMock()
        c_h_fid.table_meta_id = "t1"; c_h_fid.column_name = "FID"
        c_h_fid.is_primary_key = True; c_h_fid.data_type = "int"; c_h_fid.ordinal = 1
        c_h_date = MagicMock()
        c_h_date.table_meta_id = "t1"; c_h_date.column_name = "FDATE"
        c_h_date.is_primary_key = False; c_h_date.data_type = "date"; c_h_date.ordinal = 2
        c_l_fid = MagicMock()
        c_l_fid.table_meta_id = "t2"; c_l_fid.column_name = "FID"
        c_l_fid.is_primary_key = True; c_l_fid.data_type = "int"; c_l_fid.ordinal = 1
        c_l_qty = MagicMock()
        c_l_qty.table_meta_id = "t2"; c_l_qty.column_name = "FREALQTY"
        c_l_qty.is_primary_key = False; c_l_qty.data_type = "decimal(18,4)"; c_l_qty.ordinal = 2
        c_l_amt = MagicMock()
        c_l_amt.table_meta_id = "t2"; c_l_amt.column_name = "F_PAEZ_BHSAMOUNT"
        c_l_amt.is_primary_key = False; c_l_amt.data_type = "decimal(18,4)"; c_l_amt.ordinal = 3

        rel = MagicMock()
        rel.source_table_meta_id = "t2"; rel.target_table_meta_id = "t1"
        rel.source_columns = ["FID"]; rel.target_columns = ["FID"]

        def fake_query(model):
            q = MagicMock()
            if model is KBTableMeta:
                q.filter.return_value.all.return_value = [t_header, t_line]
            elif model is KBColumnMeta:
                q.filter.return_value.order_by.return_value.all.return_value = [
                    c_h_fid, c_h_date, c_l_fid, c_l_qty, c_l_amt,
                ]
            elif model is KBTableRelation:
                q.filter.return_value.all.return_value = [rel]
            return q

        db = MagicMock()
        db.query.side_effect = fake_query

        out = _build_schema_slice(db, ["kb-1"])
        self.assertIn("kb-1", out)
        slice_str = out["kb-1"]
        self.assertTrue(slice_str.startswith("[schema: "))
        self.assertIn("erp_t_sal_outstock", slice_str)
        # Fact table carries the role marker + measures; header carries the
        # date hint; the join edge is present.
        self.assertIn("erp_t_sal_outstockentry*", slice_str)
        self.assertIn("dt=FDATE", slice_str)
        self.assertIn("measures:FREALQTY", slice_str)
        self.assertIn("joins:erp_t_sal_outstockentry.FID→erp_t_sal_outstock.FID", slice_str)
        self.assertLess(len(slice_str), 300, f"slice too long: {len(slice_str)} chars")

    def test_schema_slice_returns_empty_on_error(self):
        from app.services.data_source_runtime.data_source_runtime import (
            _build_schema_slice,
        )
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        self.assertEqual(_build_schema_slice(db, ["kb-1"]), {})

    def test_prompt_section_includes_schema_hint(self):
        from app.services.data_source_runtime.data_source_runtime import (
            _build_data_source_prompt_section,
        )
        bound_meta = [
            {"id": "kb-1", "name": "db_zhanlu_no1", "db_type": "sqlserver",
             "database_name": "erp", "source_kind": "database",
             "file_type": "", "indexing_status": "done", "chunk_count": 10},
        ]
        section = _build_data_source_prompt_section(
            bound_meta, schema_slices={"kb-1": "[schema: erp_t_sal_outstock(FDATE)]"}
        )
        self.assertIn("Schema hint", section)
        self.assertIn("[schema: erp_t_sal_outstock(FDATE)]", section)

    def test_sub_agent_prompt_embeds_schema_hint(self):
        from app.services.tool_handlers import delegation_tools

        question = (
            "What were July 2026 sales? "
            "[schema: erp_t_sal_outstock(FDATE)→erp_t_sal_outstockentry(FID→h.FID,FREALQTY)]"
        )
        prompt = delegation_tools._build_sub_agent_prompt(
            question=question,
            bound_kb_ids=["kb-1"],
            preferred_kb="kb-1",
        )
        self.assertIn("SCHEMA HINT", prompt)
        self.assertIn("skip describe_schema", prompt)
        self.assertIn("[schema:", prompt)

    def test_data_agent_prompt_allows_schema_skip(self):
        from app.services.agent_definitions import DATA_AGENT_PROMPT
        self.assertIn("skip describe_schema", DATA_AGENT_PROMPT.lower())

    def test_delegation_with_schema_slice_makes_no_discovery_calls(self):
        """End-to-end: a question carrying a [schema: ...] block, with the
        sub-model going straight to execute_query, produces ZERO
        describe_schema calls in the tool log."""
        from app.services.tool_handlers import delegation_tools
        from app.services import agent_tools

        rows = [{"product": "Widget", "revenue": 100.0}]
        tool_log: list[str] = []

        async def fake_call_llm(messages, tools=None, endpoint=None):
            return {"content": "", "tool_calls": [_tool_call("tc1")]}

        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            tool_log.append(tool_name)
            return _make_execute_query_result(rows)

        async def fake_gate(*args, **kwargs):
            return "", ""

        db = _db_with_kb()
        with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
             patch.object(delegation_tools, "_sub_loop_answer_gate", side_effect=fake_gate), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
            result = asyncio.run(delegation_tools._ask_data_agent(
                args={
                    "question": (
                        "July 2026 sales by product "
                        "[schema: erp_t_sal_outstock(FDATE)→erp_t_sal_outstockentry"
                        "(FID→h.FID,FREALQTY)]"
                    ),
                    "data_source_id": "kb-1",
                    "max_iterations": 2,
                },
                db=db, user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))

        self.assertNotIn("describe_schema", tool_log)
        self.assertEqual(result["rows"], rows)
        self.assertTrue(result["success"])


class TestWallClockCap(unittest.TestCase):
    """(d) A slow sub-model must not hold the turn hostage."""

    def test_truncates_and_preserves_rows(self):
        from app.services.tool_handlers import delegation_tools
        from app.services import agent_tools

        rows = [{"material_name": "X", "total_revenue": 1.0}]

        async def slow_llm(messages, tools=None, endpoint=None):
            await asyncio.sleep(0.3)  # simulate a slow sub-model call
            return {"content": "", "tool_calls": [_tool_call("tc1")]}

        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            return _make_execute_query_result(rows)

        async def fake_gate(*args, **kwargs):
            return "", ""

        db = _db_with_kb()
        with patch.object(delegation_tools, "_call_llm", side_effect=slow_llm), \
             patch.object(delegation_tools, "_sub_loop_answer_gate", side_effect=fake_gate), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool), \
             patch.object(delegation_tools, "DATA_AGENT_BUDGET_SECONDS", 0.2):
            start = time.monotonic()
            result = asyncio.run(delegation_tools._ask_data_agent(
                args={"question": "top materials", "data_source_id": "kb-1",
                      "max_iterations": 4},
                db=db, user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))
            elapsed = time.monotonic() - start

        self.assertTrue(result["truncated"])
        # iter 1 completes the slow LLM call (0.3s > 0.2s budget), then the
        # top-of-iteration-2 check truncates. The synthesis turn is skipped
        # when truncated, so no extra iteration is counted.
        self.assertEqual(result["iterations"], 2)
        # The expensive synthesis turn is skipped when truncated.
        self.assertLess(elapsed, 1.0, f"delegation took {elapsed:.2f}s despite cap")
        # Rows captured before truncation are preserved and the directive
        # fallback carries them.
        self.assertEqual(result["rows"], rows)
        self.assertIn("DATA READY", result["answer"])
        self.assertIn("material_name=X", result["answer"])


class TestMaxIterations(unittest.TestCase):
    """(e) The iteration budget is respected (default 2, hard cap 6)."""

    def _run_with_responses(self, n_responses, max_iterations=None, synthesis=None):
        from app.services.tool_handlers import delegation_tools
        from app.services import agent_tools

        rows = [{"product": "Widget", "revenue": 100.0}]
        responses = [
            {"content": "", "tool_calls": [_tool_call(f"tc{i}")]}
            for i in range(n_responses)
        ]
        if synthesis is not None:
            responses.append(synthesis)
        call_log: list[dict] = []

        async def fake_call_llm(messages, tools=None, endpoint=None):
            call_log.append({"tools": tools})
            if not responses:
                raise RuntimeError("no more LLM responses mocked")
            return responses.pop(0)

        async def fake_execute_tool(tool_name, args, db, user_id, context=None):
            return _make_execute_query_result(rows)

        async def fake_gate(*args, **kwargs):
            return "", ""

        db = _db_with_kb()
        args = {"question": "sales by product", "data_source_id": "kb-1"}
        if max_iterations is not None:
            args["max_iterations"] = max_iterations
        with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
             patch.object(delegation_tools, "_sub_loop_answer_gate", side_effect=fake_gate), \
             patch.object(agent_tools, "execute_tool", side_effect=fake_execute_tool):
            result = asyncio.run(delegation_tools._ask_data_agent(
                args=args, db=db, user_id="u-1",
                context={"bound_kb_ids": ["kb-1"]},
            ))
        return result, call_log

    def test_default_is_two_iterations(self):
        result, calls = self._run_with_responses(
            2, synthesis={"content": "Final answer.", "tool_calls": []},
        )
        # 2 loop iterations + 1 forced synthesis turn = 3 LLM calls. The
        # `iterations` counter includes the synthesis turn (line 501 of
        # delegation_tools.py), so it reads 3 — the loop itself ran exactly
        # the requested 2.
        self.assertEqual(len(calls), 3, f"expected 3 LLM calls, got {len(calls)}")
        self.assertEqual(result["iterations"], 3)
        self.assertIn("Final answer", result["answer"])

    def test_hard_cap_is_six(self):
        result, calls = self._run_with_responses(
            7, max_iterations=10, synthesis={"content": "Done.", "tool_calls": []},
        )
        # 10 requested -> clamped to 6 loop iterations + 1 synthesis = 7
        # LLM calls. The loop must never exceed the 6-iteration hard cap.
        self.assertEqual(result["iterations"], 7, f"cap not enforced: {result['iterations']}")
        self.assertEqual(len(calls), 7, f"expected 7 LLM calls, got {len(calls)}")
        self.assertEqual(result["rows"], [{"product": "Widget", "revenue": 100.0}])

    def test_never_more_than_requested(self):
        result, calls = self._run_with_responses(
            2, max_iterations=1, synthesis={"content": "Short.", "tool_calls": []},
        )
        # 1 loop iteration (+ 1 synthesis turn) — the loop stopped after the
        # single requested iteration even though more tool-call responses
        # were available.
        self.assertEqual(result["iterations"], 2)
        self.assertEqual(len(calls), 2, f"expected 2 LLM calls, got {len(calls)}")


class TestWallClockCapDefault(unittest.TestCase):
    """2026-08-25: the default wall-clock cap was lowered 90s → 60s.

    A slow query must not hold the SSE stream hostage. The default should
    now be 60s (override via DATA_AGENT_BUDGET_SECONDS env var). The
    existing TestWallClockCap test patches the value to 0.2s, so it's
    unaffected by the default change; this test pins the new default.
    """

    def test_default_budget_is_60s(self):
        from app.services.tool_handlers import delegation_tools
        # Import-time read of os.environ; verify the in-memory constant.
        # Note: env var override still takes precedence (covered by the
        # patched test above), but the module-level default must be 60.0.
        self.assertEqual(
            delegation_tools.DATA_AGENT_BUDGET_SECONDS,
            60.0,
            f"DATA_AGENT_BUDGET_SECONDS default should be 60.0, "
            f"got {delegation_tools.DATA_AGENT_BUDGET_SECONDS}",
        )

    def test_env_override_still_works(self):
        """The env var DATA_AGENT_BUDGET_SECONDS still overrides the
        default at import time. We re-import the module in a subprocess
        to verify the env-var path."""
        import subprocess
        result = subprocess.run(
            [
                sys.executable, "-c",
                "import os; os.environ['DATA_AGENT_BUDGET_SECONDS']='42';"
                "import sys; sys.path.insert(0, '" + _BACKEND_ROOT + "');"
                "from app.services.tool_handlers import delegation_tools;"
                "print(delegation_tools.DATA_AGENT_BUDGET_SECONDS)",
            ],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(float(result.stdout.strip()), 42.0)


if __name__ == "__main__":
    unittest.main()
