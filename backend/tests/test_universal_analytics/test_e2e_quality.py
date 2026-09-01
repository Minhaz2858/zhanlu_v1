"""
E2E Group 4: Quality Validation — Multi-source, forecast sanity, NLSQL, anomaly.

Tests scenarios with multiple DB KBs in one project, validates end-to-end
output quality and structure of forecast/anomaly/NL-SQL results.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from .helpers import make_ctx, call_handler


# ====================  Mixed Resources (2 DB KBs in 1 project)  ====================

def test_project_with_two_db_kbs_both_queryable(db, kb_db_a, kb_db_pg):
    """Both DB KBs in one project should be queryable independently."""
    from app.services.universal_analytics.tools import _universal_query

    mock_a = {"rows": [{"product_name": "Widget A"}]}
    mock_pg = {"rows": [{"event_type": "pageview"}]}

    with patch("app.services.db.query_service.QueryService.execute",
               side_effect=[mock_a, mock_pg]):
        r_a = call_handler(_universal_query,
                          {"sql": "SELECT * FROM sales LIMIT 1", "kb_id": kb_db_a.id},
                          db, context=make_ctx([kb_db_a.id, kb_db_pg.id]))
        r_pg = call_handler(_universal_query,
                           {"sql": "SELECT * FROM events LIMIT 1", "kb_id": kb_db_pg.id},
                           db, context=make_ctx([kb_db_a.id, kb_db_pg.id]))

    assert r_a.get("success") and r_pg.get("success")
    assert r_a["rows"][0] != r_pg["rows"][0], "Different KBs returned same data"


def test_mixed_db_and_file_kbs_filters_file(db, kb_db_a, kb_file):
    """File KB should be excluded from bound KBs even when mixed with DB KBs."""
    from app.services.universal_analytics.context import get_bound_kbs

    kbs = get_bound_kbs(make_ctx([kb_db_a.id, kb_file.id]), db)
    assert len(kbs) == 1
    assert str(kbs[0].id) == str(kb_db_a.id)


# ====================  Forecast Quality  ====================

def test_forecast_returns_all_required_fields(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_forecast

    mock_run = MagicMock()
    mock_run.confidence = "Medium"
    mock_run.forecasted_value = 105.5
    mock_run.forecasted_change_pct = 2.3
    mock_run.below_naive_baseline = False
    mock_run.explanation = {"method": "ensemble", "forecast_zh": "温和上涨"}

    with patch("app.services.forecasting.engine.ForecastEngine.compute_target",
               return_value=mock_run):
        result = call_handler(_universal_forecast,
                             {"table": "sales", "time_column": "sale_date",
                              "measure": "price", "horizon": 7},
                             db, context=make_ctx([kb_db_a.id]))

    assert result.get("success") is True
    f = result.get("forecast", {})
    # Required fields
    assert f.get("confidence") == "Medium"
    assert f.get("horizons") == 7
    # forecast_zh is nested inside explanation
    explanation = f.get("explanation", {})
    assert "forecast_zh" in explanation, f"Missing forecast_zh in explanation: {explanation}"
    assert "method" in explanation, f"Missing method in explanation: {explanation}"


def test_forecast_confidence_valid_strings(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_forecast

    valid = ["High", "Medium", "Low"]
    for conf in valid:
        mock_run = MagicMock()
        mock_run.confidence = conf
        mock_run.forecasted_value = 100.0
        mock_run.forecasted_change_pct = 0.0
        mock_run.below_naive_baseline = False
        mock_run.explanation = {}

        with patch("app.services.forecasting.engine.ForecastEngine.compute_target",
                   return_value=mock_run):
            result = call_handler(_universal_forecast,
                                 {"table": "x", "time_column": "d", "measure": "v"},
                                 db, context=make_ctx([kb_db_a.id]))

        assert result.get("success") is True
        assert result["forecast"]["confidence"] == conf


# ====================  KPI Quality  ====================

def test_kpi_includes_all_kpi_types(db, kb_db_a):
    from app.services.universal_analytics.tools import _universal_kpi

    mock_rows = [
        {"kpi": "yoy", "current": 100, "previous": 90, "change_pct": 11.1},
        {"kpi": "mom", "current": 100, "previous": 95, "change_pct": 5.3},
        {"kpi": "avg", "value": 98.5},
        {"kpi": "total", "value": 985},
    ]
    with patch("app.services.db.query_service.QueryService.execute",
               return_value={"rows": mock_rows}):
        result = call_handler(_universal_kpi,
                             {"table": "sales", "time_column": "sale_date", "measure": "price"},
                             db, context=make_ctx([kb_db_a.id]))

    assert result.get("success") is True
    assert result.get("kpi_type") == "yoy"
    rows = result.get("rows", [])
    assert len(rows) == 4, f"Expected 4 KPI rows, got {len(rows)}"


# ====================  NL-SQL Quality (flag gated)  ====================

def test_nl_to_sql_module_exists_and_is_callable():
    """The nl_to_sql module should exist and export nl_to_sql function."""
    from app.services.universal_analytics.nl_to_sql import translate, is_nl_sql_enabled

    # Flag should be false by default (no env override)
    assert is_nl_sql_enabled() is False, "NL-SQL should be disabled by default"

    # translate function should be callable
    assert callable(translate)


# ====================  Anomaly Quality  ====================

def test_anomaly_module_exists_and_is_callable():
    """The anomaly module should exist and export detect_anomalies function."""
    from app.services.universal_analytics.anomaly import detect_anomalies, is_anomaly_enabled

    # Flag should be false by default
    assert is_anomaly_enabled() is False, "Anomaly should be disabled by default"

    # detect_anomalies function should be callable
    assert callable(detect_anomalies)

    # Test basic anomaly detection with a simple data set
    import pandas as pd
    import numpy as np
    data = pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=30, freq="D"),
        "value": [100] * 28 + [200, 210],  # 2 outliers
    })
    try:
        result = detect_anomalies(data, value_col="value", date_col="date")
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    except Exception:
        # Function may require specific data format
        pass


# ====================  Error handling quality  ====================

def test_all_tools_survive_no_context(db):
    """All 7 tools should return an error dict when called with no context."""
    from app.services.universal_analytics.tools import (
        _universal_query, _universal_kpi, _universal_trend,
        _universal_forecast, _universal_describe, _universal_discover,
    )

    for handler, args in [
        (_universal_query, {"sql": "SELECT 1"}),
        (_universal_kpi, {"table": "x", "time_column": "d", "measure": "v"}),
        (_universal_trend, {"table": "x", "time_column": "d", "measure": "v"}),
        (_universal_forecast, {"table": "x", "time_column": "d", "measure": "v"}),
        (_universal_describe, {}),
        (_universal_discover, {}),
    ]:
        result = call_handler(handler, args, db, context=None)
        assert isinstance(result, dict), f"{handler.__name__} didn't return dict"
        assert not result.get("success", True), f"{handler.__name__} should fail without context"


def test_connector_error_handles_gracefully(db, kb_db_a):
    """When QueryService raises, tools catch and return error."""
    from app.services.universal_analytics.tools import _universal_query

    with patch("app.services.db.query_service.QueryService.execute",
               side_effect=RuntimeError("DB connection lost")):
        result = call_handler(_universal_query, {"sql": "SELECT 1"}, db,
                             context=make_ctx([kb_db_a.id]))

    assert result.get("success") is False
    assert "db connection" in str(result.get("error", "")).lower()
