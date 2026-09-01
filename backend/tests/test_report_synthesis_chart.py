"""Tests for the chart-quality wiring in report_synthesis.

These tests pin down the contract end-to-end after the chart_quality
module was introduced:

- A degenerate LLM chart (the "all bars = 1" screenshot bug) is
  dropped and surfaced as a warning in :func:`_safe_payload_from_dict`.
- A valid LLM chart passes through unchanged.
- The fallback payload aggregates duplicate x-labels by summing
  y values, even when the source rows are pre-aggregation rows.
- A fallback whose value column is constant results in ``chart=None``
  with a constant-warning — exactly the same shape of bug the user
  hit in the screenshots.
- :func:`_fallback_payload` falls back to the shared picker when the
  task-specific pattern matching misses, instead of grabbing the
  first numeric column (which is often an id).
"""

from __future__ import annotations

import os
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


# ---------------------------------------------------------------------------
# _safe_payload_from_dict — LLM payload repair
# ---------------------------------------------------------------------------


class TestSafePayloadDropsDegenerateLLMChart(unittest.TestCase):
    """The exact screenshot bug: LLM emitted a chart with all values=1."""

    def test_constant_value_series_is_dropped(self):
        from app.services.synexia.report_synthesis import _safe_payload_from_dict

        # This is the kind of garbage the LLM produced for the
        # "数据库表分布（按模块）" snapshot: 100 rows of table names, each
        # with a synthetic count of 1.
        rows = [
            {"table_name": "accounting periods", "count": 1},
            {"table_name": "customer profiles", "count": 1},
            {"table_name": "customers", "count": 1},
            {"table_name": "customer lifetime value", "count": 1},
            {"table_name": "customer feedback", "count": 1},
        ]
        d = {
            "title": "Feasibility",
            "summary": "Tables by module.",
            "kpis": [{"label": "Tables", "value": "199"}],
            "chart": {
                "type": "bar",
                "title": "数据库表分布（按模块）",
                "x_key": "table_name",
                "y_keys": ["count"],
                "data": rows,
                "unit": "",
            },
            "insights": [],
        }
        payload = _safe_payload_from_dict(d, default_title="Feasibility")
        self.assertIsNone(payload.chart, "constant-value chart must be dropped")
        joined = " ".join(payload.warnings or []).lower()
        self.assertTrue(
            "constant" in joined or "uninform" in joined,
            f"expected a constant/uninformative warning, got {payload.warnings!r}",
        )


class TestSafePayloadValidChartPasses(unittest.TestCase):
    def test_valid_chart_is_preserved(self):
        from app.services.synexia.report_synthesis import _safe_payload_from_dict

        d = {
            "title": "Top materials",
            "summary": "A wins by 3x.",
            "kpis": [{"label": "Top", "value": "A"}],
            "chart": {
                "type": "bar",
                "title": "Top materials by revenue",
                "x_key": "material",
                "y_keys": ["revenue"],
                "data": [
                    {"material": "A", "revenue": 100.0},
                    {"material": "B", "revenue": 50.0},
                ],
                "unit": "CNY",
            },
            "insights": [],
        }
        payload = _safe_payload_from_dict(d, default_title="X")
        self.assertIsNotNone(payload.chart)
        self.assertEqual(payload.chart.x_key, "material")
        self.assertEqual(payload.chart.y_keys, ["revenue"])
        # No repair warnings on a valid chart
        joined = " ".join(payload.warnings or []).lower()
        self.assertNotIn("constant", joined)
        self.assertNotIn("missing", joined)


class TestSafePayloadAggregatesDuplicates(unittest.TestCase):
    def test_duplicate_x_labels_summed_in_llm_chart(self):
        from app.services.synexia.report_synthesis import _safe_payload_from_dict

        d = {
            "title": "By region",
            "summary": "EMEA dominates.",
            "chart": {
                "type": "bar",
                "title": "Revenue by region",
                "x_key": "region",
                "y_keys": ["revenue"],
                "data": [
                    {"region": "EMEA", "revenue": 100},
                    {"region": "EMEA", "revenue": 50},
                    {"region": "APAC", "revenue": 80},
                ],
            },
            "insights": [],
        }
        payload = _safe_payload_from_dict(d, default_title="X")
        self.assertIsNotNone(payload.chart)
        self.assertEqual(len(payload.chart.data), 2)
        emea = next(r for r in payload.chart.data if r["region"] == "EMEA")
        self.assertEqual(emea["revenue"], 150.0)
        joined = " ".join(payload.warnings or []).lower()
        self.assertTrue("aggreg" in joined, f"expected aggregation warning, got {payload.warnings!r}")


# ---------------------------------------------------------------------------
# _fallback_payload — heuristic guarantee path
# ---------------------------------------------------------------------------


class TestFallbackPayloadAggregates(unittest.TestCase):
    def test_duplicate_x_labels_aggregated(self):
        from app.services.synexia.report_synthesis import _fallback_payload

        rows = [
            {"region": "EMEA", "revenue": 100},
            {"region": "EMEA", "revenue": 50},
            {"region": "EMEA", "revenue": 25},
            {"region": "APAC", "revenue": 80},
        ]
        payload = _fallback_payload(
            user_message="Revenue by region",
            rows=rows,
            sql="SELECT region, revenue FROM sales",
            source_name="erp",
            user_signal="default",
            task_type="sales",
        )
        self.assertIsNotNone(payload.chart)
        self.assertEqual(len(payload.chart.data), 2)
        emea = next(r for r in payload.chart.data if r["region"] == "EMEA")
        self.assertEqual(emea["revenue"], 175.0)


class TestFallbackPayloadConstantValueDropped(unittest.TestCase):
    def test_constant_value_column_yields_no_chart(self):
        from app.services.synexia.report_synthesis import _fallback_payload

        # Snapshot: 100 rows of tables, each with a synthetic "id" column
        # the heuristic mistakenly picks. id is a primary key, which is
        # a series of distinct integers — but the *count* of tables per
        # region (if we had grouped) is 1 for every region, i.e.
        # constant. The test uses a more direct setup: rows have only
        # a label and a value of 1, simulating the all-ones bug.
        rows = [
            {"region": "APAC", "value": 1},
            {"region": "EMEA", "value": 1},
            {"region": "NA", "value": 1},
        ]
        payload = _fallback_payload(
            user_message="Tables by region",
            rows=rows,
            sql="SELECT region, 1 AS value FROM tables",
            source_name="erp",
            user_signal="default",
            task_type="general",
        )
        self.assertIsNone(payload.chart)
        joined = " ".join(payload.warnings or []).lower()
        self.assertTrue(
            "constant" in joined or "uninform" in joined,
            f"expected constant/uninformative warning, got {payload.warnings!r}",
        )


class TestFallbackPayloadSkipsIdColumnForValue(unittest.TestCase):
    def test_id_column_not_used_as_value(self):
        from app.services.synexia.report_synthesis import _fallback_payload

        rows = [
            {"id": 1, "region": "APAC", "amount": 10},
            {"id": 2, "region": "EMEA", "amount": 20},
            {"id": 3, "region": "NA", "amount": 30},
        ]
        payload = _fallback_payload(
            user_message="By region",
            rows=rows,
            sql="SELECT id, region, amount FROM t",
            source_name="erp",
            user_signal="default",
            task_type="general",
        )
        self.assertIsNotNone(payload.chart)
        # The value column must be the meaningful numeric one, not id
        self.assertNotIn("id", payload.chart.y_keys)


# ---------------------------------------------------------------------------
# synthesize_report end-to-end (LLM is mocked)
# ---------------------------------------------------------------------------


class TestSynthesizeReportWithMockedLLM(unittest.TestCase):
    """The LLM is patched to return a degenerate chart → result must
    still be safe to render (chart=None, warnings populated)."""

    def test_degenerate_llm_chart_is_dropped(self):
        import asyncio
        from app.services.synexia.report_synthesis import synthesize_report

        async def fake_llm(system, messages):
            return {
                "content": (
                    "Here's the analysis.\n"
                    "```json\n"
                    + '{"title": "X", "summary": "Y", "chart": '
                    + '{"type": "bar", "title": "T", "x_key": "name", '
                    + '"y_keys": ["v"], '
                    + '"data": [{"name": "a", "v": 1}, {"name": "b", "v": 1}, '
                    + '{"name": "c", "v": 1}, {"name": "d", "v": 1}, '
                    + '{"name": "e", "v": 1}]}}\n'
                    + "```\n"
                )
            }

        async def run() -> None:
            return await synthesize_report(
                user_message="analyze these",
                rows=[
                    {"name": "a", "v": 1},
                    {"name": "b", "v": 1},
                    {"name": "c", "v": 1},
                    {"name": "d", "v": 1},
                    {"name": "e", "v": 1},
                ],
                sql="SELECT * FROM t",
                source_name="erp",
                source_id="src1",
                call_llm_fn=fake_llm,
                user_signal="default",
            )

        result = asyncio.run(run())
        self.assertIsNotNone(result.report_card_payload)
        self.assertIsNone(result.report_card_payload.chart)
        joined = " ".join(result.report_card_payload.warnings or []).lower()
        self.assertTrue("constant" in joined or "uninform" in joined)


if __name__ == "__main__":
    unittest.main()
