"""
E2E Group 1: Full Pipeline — discover → describe → query → KPI → trend → forecast.

Tests the complete async tool chain end-to-end on database KBs.
Mocks at the QueryService / SchemaService / ForecastEngine level.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from .helpers import make_ctx, call_handler


# ====================  describe  ====================

def test_describe_all_tables_returns_schema(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_describe

    mock_schema = {"success": True, "tables": {"sales": {"id": "int", "price": "decimal"}}}
    with patch("app.services.db.schema_service.SchemaService.describe_all",
               return_value=mock_schema["tables"]):
        result = call_handler(_universal_describe, {}, db,
                             context=make_ctx([kb_db_a.id]))

    assert result.get("success") is True, f"Expected success: {result}"
    assert "tables" in result, f"Expected tables key: {result}"


def test_describe_specific_table(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_describe

    mock_info = [{"name": "id", "type": "int"}, {"name": "price", "type": "decimal"}]
    with patch("app.services.db.schema_service.SchemaService.describe_table",
               return_value=mock_info):
        result = call_handler(_universal_describe, {"table": "sales"}, db,
                             context=make_ctx([kb_db_a.id]))

    assert result.get("success") is True
    assert "schema" in result
    assert len(result["schema"]) == 2


def test_describe_no_bound_kb_returns_error(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_describe

    result = call_handler(_universal_describe, {}, db, context=make_ctx([]))
    assert result.get("success") is False
    assert "no database" in str(result.get("error", "")).lower()


# ====================  discover  ====================

def test_discover_returns_candidates(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_discover

    mock_candidates = [
        {"table": "sales", "time_column": "sale_date", "measure": "price", "row_count": 100}
    ]
    with patch("app.services.forecasting.discovery.discover", return_value=mock_candidates):
        result = call_handler(_universal_discover, {}, db,
                             context=make_ctx([kb_db_a.id]))

    assert result.get("success") is True, f"Expected success: {result}"
    assert result.get("count") == 1
    assert result["candidates"][0]["table"] == "sales"


def test_discover_no_db_returns_error(db):
    from app.services.universal_analytics.tools import _universal_discover

    result = call_handler(_universal_discover, {}, db, context=make_ctx([]))
    assert result.get("success") is False


# ====================  query  ====================

def test_query_returns_rows(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_query

    mock_result = {"rows": [{"id": 1, "price": 100.5}, {"id": 2, "price": 101.0}]}
    with patch("app.services.db.query_service.QueryService.execute",
               return_value=mock_result):
        result = call_handler(_universal_query,
                             {"sql": "SELECT * FROM sales LIMIT 2"}, db,
                             context=make_ctx([kb_db_a.id]))

    assert result.get("success") is True
    assert len(result.get("rows", [])) == 2


def test_query_no_bound_kb_returns_error(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_query

    result = call_handler(_universal_query, {"sql": "SELECT 1"}, db,
                         context=make_ctx([]))
    assert result.get("success") is False


def test_query_destructive_sql_rejected(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_query

    for sql in ["DROP TABLE x", "DELETE FROM x", "INSERT INTO x VALUES(1)"]:
        result = call_handler(_universal_query, {"sql": sql}, db,
                             context=make_ctx([kb_db_a.id]))
        assert result.get("success") is False, f"Should reject: {sql}"
        assert any(t in str(result.get("error", "")).lower()
                   for t in ("not allowed", "read-only", "select", "forbidden", "invalid"))


def test_query_nonexistent_kb(db):
    from app.services.universal_analytics.tools import _universal_query

    result = call_handler(_universal_query,
                         {"sql": "SELECT 1", "kb_id": "nonexistent-999"}, db,
                         context=make_ctx(["nonexistent-999"]))
    assert result.get("success") is False


# ====================  KPI  ====================

def test_kpi_returns_results(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_kpi

    mock_rows = [{"kpi": "yoy", "current": 105, "previous": 100, "change_pct": 5.0}]
    with patch("app.services.db.query_service.QueryService.execute",
               return_value={"rows": mock_rows}):
        result = call_handler(_universal_kpi,
                             {"table": "sales", "time_column": "sale_date", "measure": "price"},
                             db, context=make_ctx([kb_db_a.id]))

    assert result.get("success") is True, f"Expected success: {result}"
    assert result.get("kpi_type") == "yoy"
    assert len(result.get("rows", [])) >= 1


def test_kpi_missing_required_fields(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_kpi

    result = call_handler(_universal_kpi, {"table": "sales"}, db,
                         context=make_ctx([kb_db_a.id]))
    assert result.get("success") is False
    assert "required" in str(result.get("error", "")).lower()


def test_kpi_no_bound_kb(db):
    from app.services.universal_analytics.tools import _universal_kpi

    result = call_handler(_universal_kpi,
                         {"table": "x", "time_column": "d", "measure": "v"},
                         db, context=make_ctx([]))
    assert result.get("success") is False


# ====================  trend  ====================

def test_trend_returns_analysis(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_trend

    mock_series = pd_series(15, freq="D")
    with patch("app.services.forecasting.datasource_registry.GenericKBStrategy.fetch",
               return_value=mock_series):
        result = call_handler(_universal_trend,
                             {"table": "sales", "time_column": "sale_date", "measure": "price"},
                             db, context=make_ctx([kb_db_a.id]))

    assert result.get("success") is True, f"Expected success: {result}"
    assert "trend" in result


def test_trend_no_data_returns_error(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_trend

    with patch("app.services.forecasting.datasource_registry.GenericKBStrategy.fetch",
               return_value=None):
        result = call_handler(_universal_trend,
                             {"table": "sales", "time_column": "sale_date", "measure": "price"},
                             db, context=make_ctx([kb_db_a.id]))
    assert result.get("success") is False


def test_trend_no_bound_kb(db):
    from app.services.universal_analytics.tools import _universal_trend

    result = call_handler(_universal_trend,
                         {"table": "x", "time_column": "d", "measure": "v"},
                         db, context=make_ctx([]))
    assert result.get("success") is False


# ====================  forecast  ====================

def test_forecast_with_table_args(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_forecast

    mock_run = MagicMock()
    mock_run.confidence = "High"
    mock_run.explanation = {"method": "ensemble"}
    mock_run.forecasted_value = 105.5
    mock_run.forecasted_change_pct = 2.3

    with patch("app.services.forecasting.engine.ForecastEngine.compute_target",
               return_value=mock_run):
        result = call_handler(_universal_forecast,
                             {"table": "sales", "time_column": "sale_date",
                              "measure": "price", "horizon": 7},
                             db, context=make_ctx([kb_db_a.id]))

    assert result.get("success") is True, f"Expected success: {result}"
    f = result.get("forecast", {})
    assert f.get("confidence") == "High"
    assert f.get("horizons") == 7


def test_forecast_no_bound_kb(db):
    from app.services.universal_analytics.tools import _universal_forecast

    result = call_handler(_universal_forecast,
                         {"table": "x", "time_column": "d", "measure": "v"},
                         db, context=make_ctx([]))
    assert result.get("success") is False


def test_forecast_missing_args(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_forecast

    result = call_handler(_universal_forecast, {}, db, context=make_ctx([kb_db_a.id]))
    assert result.get("success") is False
    assert "provide" in str(result.get("error", "")).lower()


# ====================  Quality: KPI values  ====================

def test_kpi_output_structure_valid(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_kpi

    mock_rows = [
        {"kpi": "yoy", "current": 100, "previous": 90, "change_pct": 11.1}
    ]
    with patch("app.services.db.query_service.QueryService.execute",
               return_value={"rows": mock_rows}):
        result = call_handler(_universal_kpi,
                             {"table": "sales", "time_column": "sale_date", "measure": "price"},
                             db, context=make_ctx([kb_db_a.id]))

    assert result.get("success") is True
    assert "sql" in result  # KPI handler includes the generated SQL


# ====================  Quality: Trend structure  ====================

def test_trend_includes_slope_and_direction(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_trend

    mock_series = pd_series(20, freq="D")
    with patch("app.services.forecasting.datasource_registry.GenericKBStrategy.fetch",
               return_value=mock_series):
        result = call_handler(_universal_trend,
                             {"table": "sales", "time_column": "sale_date", "measure": "price",
                              "window": 3},
                             db, context=make_ctx([kb_db_a.id]))

    if result.get("success"):
        trend = result.get("trend", {})
        assert isinstance(trend, dict), f"Trend should be dict: {trend}"
        # analyze_trend returns direction/slope/strength fields
        trend_keys = {k.lower() for k in trend.keys()}
        assert any(k in trend_keys for k in {"direction", "slope", "strength", "r2"}), \
            f"Missing trend analysis keys: {list(trend.keys())}"


# ====================  Helpers  ====================

def pd_series(n, freq="D"):
    """Create a pandas Series with date index for trend analysis."""
    import pandas as pd
    import numpy as np
    dates = pd.date_range("2025-01-01", periods=n, freq=freq)
    return pd.Series(np.linspace(100, 120, n) + np.random.randn(n) * 2, index=dates)
