"""Tests for generated dashboard ``metric_dimensions`` shape tolerance.

2026-09-03 regression: metrics whose ``options.filters`` is a DICT of
date-window filters ({"date": {"column": ..., "op": ..., "value":
":dim_date"}}) crashed the list comprehension with ``AttributeError: 'str'
object has no attribute 'get'`` — iterating a dict yields bare string keys.
Every daily-sales-data-sync metric poll logged a full traceback each 30 s
refresh interval.

The fix normalizes dict filters (dict key = declared token name) in the
template AND every generated ``queries.py``; these tests pin the generated
runtime behaviour on the real generated modules.
"""

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


class TestMetricDimensionsDictFilters:
    def test_dict_date_filter_no_crash_and_declares_dim(self):
        """A dict filter {date: {column, op, value}} must not crash and must
        declare the :dim_date token so render_widget_sql accepts the SQL."""
        from app.dashboards.daily_sales_data_sync.queries import (
            METRICS,
            metric_dimensions,
        )

        assert METRICS, "expected generated metrics to exist"
        for m in METRICS:
            dims = metric_dimensions(m)  # must not raise
            assert isinstance(dims, list)
            # Every metric here carries a date dict-filter → dim declared.
            assert any(d.get("token") == "date" for d in dims), (m["id"], dims)

    def test_list_filters_unchanged(self):
        """List-shaped filters keep working exactly as before."""
        from app.dashboards.sales_performance_dashboard.queries import metric_dimensions

        m = {
            "id": "x",
            "options": {
                "filters": [
                    {"key": "product", "column": "FNAME"},
                    {"key": "org", "column": "ORG"},
                ]
            },
        }
        dims = metric_dimensions(m)
        assert dims == [
            {"token": "product", "column": "FNAME"},
            {"token": "org", "column": "ORG"},
        ]

    def test_no_filters_returns_empty(self):
        from app.dashboards.daily_sales_data_sync.queries import metric_dimensions

        assert metric_dimensions({"id": "y", "options": {}}) == []
