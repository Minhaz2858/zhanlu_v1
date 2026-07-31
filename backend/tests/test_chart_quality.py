"""Tests for the shared chart-quality gate.

Reproduces the screenshot bug (`数据库表分布（按模块）` showing 10 bars all at
height 1) and pins down the contract:

- Constant-value series (the actual bug) are dropped with a warning.
- Valid charts pass through unchanged.
- Duplicate x-labels are aggregated by sum.
- String numerics ("25.5%", "1,234") are coerced to float.
- Categories are capped to top-N + "Other".
- Non-existent y_keys cause the chart to be dropped.
- pick_chart_columns skips id-like / timestamp columns for the value
  column and prefers a low-cardinality string column for the label.

Pure-Python — no DB, no LLM.
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
# validate_chart_spec
# ---------------------------------------------------------------------------


class TestValidateChartSpecDropsConstantSeries(unittest.TestCase):
    """The exact screenshot bug: every bar is height 1.0 with a 0→1 axis."""

    def test_all_values_equal_to_one_is_dropped(self):
        from app.services.synexia.chart_quality import validate_chart_spec
        from app.services.synexia.contracts import ChartSpec

        # 199 row snapshot, one per DB table, each with a constant
        # "count" of 1. This is what the synthesis LLM emits when the
        # underlying snapshot has no real numeric measure — and what
        # produced the "all bars = 1" chart in the user's screenshot.
        data = [
            {"table_name": name, "count": 1}
            for name in [
                "accounting periods",
                "customer profiles",
                "customers",
                "customer lifetime value",
                "customer feedback",
                "customer invoice lines",
            ]
        ]
        chart = ChartSpec(
            type="bar",
            title="数据库表分布（按模块）",
            x_key="table_name",
            y_keys=["count"],
            data=data,
        )
        repaired, warnings = validate_chart_spec(chart)
        self.assertIsNone(
            repaired, "Constant-value series must be dropped, not rendered as flat bars"
        )
        self.assertTrue(
            any("constant" in w.lower() or "uninform" in w.lower() for w in warnings),
            f"expected a constant/uninformative warning, got {warnings!r}",
        )


class TestValidateChartSpecValidPassthrough(unittest.TestCase):
    def test_valid_chart_passes_through_unchanged(self):
        from app.services.synexia.chart_quality import validate_chart_spec
        from app.services.synexia.contracts import ChartSpec

        chart = ChartSpec(
            type="bar",
            title="Top materials",
            x_key="material",
            y_keys=["revenue"],
            data=[
                {"material": "A", "revenue": 100.0},
                {"material": "B", "revenue": 50.0},
                {"material": "C", "revenue": 25.0},
            ],
        )
        repaired, warnings = validate_chart_spec(chart)
        self.assertIsNotNone(repaired)
        self.assertEqual(len(repaired.data), 3)
        self.assertEqual(repaired.x_key, "material")
        self.assertEqual(repaired.y_keys, ["revenue"])
        self.assertEqual(warnings, [])


class TestValidateChartSpecAggregatesDuplicates(unittest.TestCase):
    def test_duplicate_x_labels_summed(self):
        from app.services.synexia.chart_quality import validate_chart_spec
        from app.services.synexia.contracts import ChartSpec

        chart = ChartSpec(
            type="bar",
            title="By region",
            x_key="region",
            y_keys=["revenue"],
            data=[
                {"region": "EMEA", "revenue": 100},
                {"region": "EMEA", "revenue": 50},
                {"region": "APAC", "revenue": 80},
            ],
        )
        repaired, warnings = validate_chart_spec(chart)
        self.assertIsNotNone(repaired)
        self.assertEqual(len(repaired.data), 2)
        emea = next(r for r in repaired.data if r["region"] == "EMEA")
        self.assertEqual(emea["revenue"], 150.0)
        self.assertTrue(
            any("duplic" in w.lower() or "aggreg" in w.lower() for w in warnings),
            f"expected an aggregation warning, got {warnings!r}",
        )


class TestValidateChartSpecCoercesStrings(unittest.TestCase):
    def test_string_percentage_coerced(self):
        from app.services.synexia.chart_quality import validate_chart_spec
        from app.services.synexia.contracts import ChartSpec

        chart = ChartSpec(
            type="bar",
            title="X",
            x_key="region",
            y_keys=["share"],
            data=[{"region": "A", "share": "25.5%"}, {"region": "B", "share": "74.5%"}],
        )
        repaired, warnings = validate_chart_spec(chart)
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.data[0]["share"], 25.5)
        self.assertEqual(repaired.data[1]["share"], 74.5)

    def test_thousands_string_coerced(self):
        from app.services.synexia.chart_quality import validate_chart_spec
        from app.services.synexia.contracts import ChartSpec

        chart = ChartSpec(
            type="bar",
            title="X",
            x_key="region",
            y_keys=["revenue"],
            data=[{"region": "A", "revenue": "1,234"}, {"region": "B", "revenue": "2,500"}],
        )
        repaired, warnings = validate_chart_spec(chart)
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.data[0]["revenue"], 1234.0)
        self.assertEqual(repaired.data[1]["revenue"], 2500.0)


class TestValidateChartSpecCapsCategories(unittest.TestCase):
    def test_more_than_max_capped_to_top_n_plus_other(self):
        from app.services.synexia.chart_quality import validate_chart_spec
        from app.services.synexia.contracts import ChartSpec

        # 20 categories, max 12 → 12 rows + 1 "Other" row. The repaired
        # chart keeps the top-12 by magnitude (largest first), so "Other"
        # is the sum of the 8 smallest values (0+1+2+3+4+5+6+7 = 28).
        rows = [{"cat": f"c{i:02d}", "val": float(i)} for i in range(20)]
        chart = ChartSpec(
            type="bar", title="X", x_key="cat", y_keys=["val"], data=rows
        )
        repaired, warnings = validate_chart_spec(chart, max_categories=12)
        self.assertIsNotNone(repaired)
        self.assertEqual(len(repaired.data), 13)
        self.assertEqual(repaired.data[-1]["cat"], "Other")
        self.assertEqual(repaired.data[-1]["val"], sum(float(i) for i in range(0, 8)))
        self.assertTrue(
            any("cap" in w.lower() or "top" in w.lower() for w in warnings),
            f"expected a cap warning, got {warnings!r}",
        )


class TestValidateChartSpecMissingYKey(unittest.TestCase):
    def test_y_key_absent_from_data_drops_chart(self):
        from app.services.synexia.chart_quality import validate_chart_spec
        from app.services.synexia.contracts import ChartSpec

        chart = ChartSpec(
            type="bar",
            title="X",
            x_key="region",
            y_keys=["nonexistent"],
            data=[{"region": "A"}, {"region": "B"}],
        )
        repaired, warnings = validate_chart_spec(chart)
        self.assertIsNone(repaired)
        self.assertTrue(
            any("y_key" in w.lower() or "missing" in w.lower() for w in warnings),
            f"expected a missing-y_key warning, got {warnings!r}",
        )


class TestValidateChartSpecEmptyData(unittest.TestCase):
    def test_empty_data_returns_none_with_warning(self):
        from app.services.synexia.chart_quality import validate_chart_spec
        from app.services.synexia.contracts import ChartSpec

        chart = ChartSpec(
            type="bar", title="X", x_key="region", y_keys=["v"], data=[]
        )
        repaired, warnings = validate_chart_spec(chart)
        self.assertIsNone(repaired)
        self.assertTrue(len(warnings) >= 1)


# ---------------------------------------------------------------------------
# pick_chart_columns
# ---------------------------------------------------------------------------


class TestPickChartColumnsSkipsIdLikeValues(unittest.TestCase):
    def test_id_column_ignored_for_value(self):
        from app.services.synexia.chart_quality import pick_chart_columns

        rows = [
            {"id": 1, "name": "A", "value": 10},
            {"id": 2, "name": "B", "value": 20},
            {"id": 3, "name": "C", "value": 30},
        ]
        label, value = pick_chart_columns(rows)
        self.assertEqual(value, "value")
        self.assertEqual(label, "name")

    def test_user_id_column_ignored_for_value(self):
        from app.services.synexia.chart_quality import pick_chart_columns

        rows = [
            {"user_id": 1, "department": "Eng", "salary": 100},
            {"user_id": 2, "department": "Sales", "salary": 200},
        ]
        label, value = pick_chart_columns(rows)
        self.assertEqual(value, "salary")
        self.assertEqual(label, "department")


class TestPickChartColumnsSkipsTimestamps(unittest.TestCase):
    def test_created_at_ignored_for_value(self):
        from app.services.synexia.chart_quality import pick_chart_columns

        # Note: created_at is a real datetime in practice; we use an ISO
        # string here because pick_chart_columns only inspects the column
        # name pattern + the *type* of the first sample, not the value
        # semantics.
        rows = [
            {"created_at": "2024-01-01", "name": "A", "amount": 10},
            {"created_at": "2024-01-02", "name": "B", "amount": 20},
        ]
        label, value = pick_chart_columns(rows)
        self.assertEqual(value, "amount")
        self.assertEqual(label, "name")


class TestPickChartColumnsPrefersLowCardinalityLabel(unittest.TestCase):
    def test_low_cardinality_string_preferred(self):
        from app.services.synexia.chart_quality import pick_chart_columns

        # uuid is a high-cardinality string, region is low-cardinality
        rows = [
            {"uuid": "a-001", "region": "EMEA", "amount": 10},
            {"uuid": "a-002", "region": "EMEA", "amount": 20},
            {"uuid": "a-003", "region": "APAC", "amount": 30},
            {"uuid": "a-004", "region": "APAC", "amount": 40},
        ]
        label, value = pick_chart_columns(rows)
        self.assertEqual(label, "region")
        self.assertEqual(value, "amount")


class TestPickChartColumnsEmptyRows(unittest.TestCase):
    def test_no_rows_returns_none_pair(self):
        from app.services.synexia.chart_quality import pick_chart_columns

        self.assertEqual(pick_chart_columns([]), (None, None))
        self.assertEqual(pick_chart_columns(None), (None, None))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
