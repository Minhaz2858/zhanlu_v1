"""FIX A+C (2026-08-22): deferred deliverable synthesizes from the FULL
answer-tagged dataset set, not just the final query.

Regression context: the July 2026 sales-report turn recorded three answer
datasets (totals, product breakdown, and a 1-row data-quality check), but
the deferred deliverable passed only ``answer_datasets()[-1]`` into
``synthesize_report`` — so the report was synthesized from a single
degenerate row and the richer datasets never reached the LLM.

This suite pins down:
- :func:`merge_answer_rows`: deterministic merge (record order kept,
  identical rows deduplicated, JSON-key stable).
- ``synthesize_report(..., datasets=...)``: the LLM payload carries a
  per-dataset overview and the merged rows, and the merge safety net
  works when the caller omits ``rows``.
"""

from __future__ import annotations

import pytest

from app.services.synexia.report_synthesis import merge_answer_rows, synthesize_report


class TestMergeAnswerRows:
    def test_dedups_identical_rows_and_preserves_first_seen_order(self):
        datasets = [
            {"rows": [{"a": 1}, {"a": 2}]},
            {"rows": [{"a": 2}, {"a": 3}]},  # {"a": 2} is a duplicate of ds1
            {"rows": [{"a": 1}]},            # {"a": 1} is a duplicate of ds1
        ]
        merged = merge_answer_rows(datasets)
        assert merged == [{"a": 1}, {"a": 2}, {"a": 3}]

    def test_handles_empty_and_missing_rows(self):
        assert merge_answer_rows([]) == []
        assert merge_answer_rows(None) == []
        assert merge_answer_rows([{"rows": None}, {"rows": []}]) == []

    def test_keeps_heterogeneous_schemas_concatenated(self):
        datasets = [
            {"rows": [{"month": "2026-07", "amount": 100}]},
            {"rows": [{"product": "A", "amount": 100}]},
        ]
        merged = merge_answer_rows(datasets)
        assert merged == [{"month": "2026-07", "amount": 100}, {"product": "A", "amount": 100}]


class TestSynthesizeReportMultiDataset:
    @staticmethod
    def _three_answer_datasets():
        return [
            {
                "source_name": "totals",
                "source_id": "s0",
                "sql": "SELECT month, sum(amount) FROM sales GROUP BY month",
                "rows": [
                    {"month": "2026-07", "amount": 100},
                    {"month": "2026-06", "amount": 90},
                ],
            },
            {
                "source_name": "breakdown",
                "source_id": "s1",
                "sql": "SELECT product, sum(amount) FROM sales GROUP BY product",
                "rows": [
                    {"product": "A", "amount": 100},
                    {"product": "B", "amount": 90},
                    {"product": "C", "amount": 80},
                ],
            },
            {
                "source_name": "quality_check",
                "source_id": "s2",
                "sql": "SELECT count(*) AS cnt FROM sales",
                "rows": [{"cnt": 3}],
            },
        ]

    @pytest.mark.asyncio
    async def test_datasets_overview_and_merged_rows_in_llm_payload(self):
        captured = {}
        datasets = self._three_answer_datasets()

        async def fake_llm(system, msgs):
            captured["user_payload"] = msgs[0]["content"]
            return {"content": "thanks"}

        result = await synthesize_report(
            user_message="July 2026 sales report",
            rows=merge_answer_rows(datasets),
            sql=datasets[-1]["sql"],
            source_name=datasets[-1]["source_name"],
            source_id=datasets[-1]["source_id"],
            call_llm_fn=fake_llm,
            datasets=datasets,
        )
        assert result.task_kind == "report"

        payload = captured["user_payload"]
        # Deterministic per-dataset overview
        assert "MULTI-DATASET CONTEXT (3 queries executed this turn)" in payload
        assert "Dataset 1/3: source=totals (id=s0), rows=2, columns=['month', 'amount']" in payload
        assert "Dataset 2/3: source=breakdown (id=s1), rows=3, columns=['product', 'amount']" in payload
        assert "Dataset 3/3: source=quality_check (id=s2), rows=1, columns=['cnt']" in payload
        # sql lines per dataset
        assert "SELECT month, sum(amount)" in payload
        assert "SELECT product, sum(amount)" in payload
        assert "SELECT count(*) AS cnt" in payload
        # The LLM sees the MERGED rows from every dataset (2+3+1 = 6)
        assert "Row count: 6" in payload
        assert '"month": "2026-07"' in payload
        assert '"product": "A"' in payload
        assert '"cnt": 3' in payload

    @pytest.mark.asyncio
    async def test_safety_net_merges_rows_when_rows_omitted(self):
        """datasets alone must be enough — rows derive from the merge."""
        captured = {}
        datasets = [
            {"source_name": "q1", "source_id": "a", "rows": [{"m": 1}]},
            {"source_name": "q2", "source_id": "b", "rows": [{"m": 2}]},
        ]

        async def fake_llm(system, msgs):
            captured["user_payload"] = msgs[0]["content"]
            return {"content": "thanks"}

        await synthesize_report(
            user_message="data",
            rows=None,
            sql=None,
            source_name=None,
            source_id=None,
            call_llm_fn=fake_llm,
            datasets=datasets,
        )

        payload = captured["user_payload"]
        assert "Row count: 2" in payload
        assert '"m": 1' in payload
        assert '"m": 2' in payload
        assert "MULTI-DATASET CONTEXT (2 queries executed this turn)" in payload

    @pytest.mark.asyncio
    async def test_no_datasets_param_keeps_legacy_payload_unchanged(self):
        """Callers that never pass datasets get the exact old payload shape."""
        captured = {}

        async def fake_llm(system, msgs):
            captured["user_payload"] = msgs[0]["content"]
            return {"content": "thanks"}

        await synthesize_report(
            user_message="data",
            rows=[{"a": 1}],
            sql="SELECT 1",
            source_name="src",
            source_id="s1",
            call_llm_fn=fake_llm,
        )

        payload = captured["user_payload"]
        assert "MULTI-DATASET CONTEXT" not in payload
        assert "Row count: 1" in payload
