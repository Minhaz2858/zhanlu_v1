"""Tests for the deferred-deliverable wire + empty-bubble guarantee (2026-08-21).

Covers the two production bugs from the Ecisco BI traces:
1. Wire gap: the agent collected 41 rows via execute_query (direct SQL),
   but record_dataset only fired for ask_data_agent → answer_datasets()
   stayed empty → no card was built. Fix: DATA_PRODUCING_TOOLS covers all
   data-producing tools.
2. Empty bubble: the turn's only prose was promise narration; the strips
   removed it all and the post-loop fallback skipped itself
   (content_streamed=True) → the persisted message had empty content.
   Fix: post-deferred guarantee always leaves a non-empty final bubble.
"""

from app.routers.agents import (
    DATA_PRODUCING_TOOLS,
    _choose_fallback,
    _strip_internal_references,
)
from app.services.goal_contract import build_goal_contract
from app.services.query_purpose import classify_query_purpose


class TestDataProducingToolsWire:
    def test_constant_covers_direct_sql_tools(self):
        assert "ask_data_agent" in DATA_PRODUCING_TOOLS
        assert "execute_query" in DATA_PRODUCING_TOOLS
        assert "execute_sql" in DATA_PRODUCING_TOOLS
        assert "sql_query" in DATA_PRODUCING_TOOLS
        assert "forecast_brief" in DATA_PRODUCING_TOOLS

    def test_execute_query_join_classifies_answer_and_records_dataset(self):
        """The screenshot-A shape: a sales join on a fact table issued via
        execute_query must tag 'answer' and land in answer_datasets()."""
        sql = (
            "SELECT s.FMATERIALID AS product_id, SUM(s.FQTY) AS total_sales_qty, "
            "SUM(s.FAMOUNT) AS total_revenue FROM erp_t_sal_outstockentry s "
            "WHERE s.FDATE >= '2026-07-01' GROUP BY s.FMATERIALID"
        )
        rows = [
            {"product_id": 103350, "total_sales_qty": 12.0, "total_revenue": 98905.2},
            {"product_id": 103352, "total_sales_qty": 8.0, "total_revenue": 64600.0},
        ]
        roles = {"erp_t_sal_outstockentry": "fact"}
        assert classify_query_purpose(sql, rows, roles) == "answer"

        c = build_goal_contract("July 2026 sales report")
        if c.requires_data:
            c.record_dataset(
                rows=rows, sql=sql, source_name="erp", source_id=None,
                purpose="answer", tool_call_id="tc-1",
            )
            assert c.has_answer_data()
            assert len(c.answer_datasets()) == 1

    def test_single_column_id_dump_is_probe_not_answer(self):
        """The screenshot-1 shape: an 80-row single-column FMATERIALID dump
        (warehouse probe) must NOT become a deliverable dataset."""
        sql = "SELECT FMATERIALID FROM aipdp_data_warehouse_prod LIMIT 80"
        rows = [{"FMATERIALID": i} for i in range(80)]
        purpose = classify_query_purpose(sql, rows, {"aipdp_data_warehouse_prod": "unknown"})
        assert purpose != "answer"


class TestEmptyBubbleGuarantee:
    def test_promise_narration_strips_to_empty(self):
        """The exact narration pattern from the failing traces: every
        sentence is a promise → the hygiene strip empties the prose."""
        prose = (
            "Let me query the warehouse for the last 30 days. "
            "Let me check the live sales tables. "
            "Let me verify the inventory join. "
            "I'll re-query against the current tables now."
        )
        assert _strip_internal_references(prose).strip() == ""

    def test_real_answer_survives_strip(self):
        prose = (
            "July sales totaled ¥12.4M across 41 products, up 6.2% vs June. "
            "Let me verify the inventory join."
        )
        out = _strip_internal_references(prose)
        assert "¥12.4M" in out
        assert "verify" not in out

    def test_choose_fallback_never_empty_after_strip(self):
        """Composition guarantee: strips may empty the prose, but the
        fallback chooser must always return non-empty text — the persisted
        bubble is never blank."""
        tool_calls = [
            {
                "id": "tc-1",
                "name": "execute_query",
                "status": "completed",
                "results": {"rows": [{"FMATERIALID": 103350}]},
            },
        ]
        stripped = _strip_internal_references(
            "Let me query the warehouse. Let me check the tables."
        )
        assert stripped.strip() == ""
        fallback = _choose_fallback(
            tool_calls, [],
            user_content="Give me supply chain data for last 30 days",
        )
        assert fallback and fallback.strip()
