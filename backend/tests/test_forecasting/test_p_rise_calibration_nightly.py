"""Test: p_rise calibration nightly step."""
import os
import pytest
import datetime
from unittest.mock import patch, MagicMock

os.environ.setdefault("FORECAST_P_RISE_CALIBRATION_ENABLED", "true")


def test_calibration_skips_on_non_monday():
    """run_weekly_p_rise_calibration skips when not Monday."""
    from app.services.forecasting.ops.p_rise_calibration import run_weekly_p_rise_calibration

    # Pass a Wednesday as _today
    result = run_weekly_p_rise_calibration(
        MagicMock(), _today=datetime.date(2026, 8, 19)
    )

    assert result.get("skipped") is True
    assert result.get("reason") == "not_monday"


def test_calibration_persists_to_model_config():
    """On Monday, with enough decisions, calibration is persisted."""
    from app.services.forecasting.ops.p_rise_calibration import run_weekly_p_rise_calibration

    db = MagicMock()

    target = MagicMock()
    target.name = "PE_LLDPE"
    target.product_key = "PE_LLDPE"
    target.model_config = {}

    decisions = []
    for i in range(25):
        d = MagicMock()
        d.predicted_p_rise = 0.3 + i * 0.02
        d.actual_price_t = 100.0
        d.actual_price_th = 105.0 if i < 12 else 95.0
        decisions.append(d)

    def mock_query(model):
        q = MagicMock()
        name = getattr(model, "__name__", "")
        if name == "ForecastTarget":
            q.filter.return_value.all.return_value = [target]
        elif name == "ForecastDecisionLog":
            q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = decisions
        else:
            q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        return q

    db.query.side_effect = mock_query

    # Pass a Monday as _today
    result = run_weekly_p_rise_calibration(
        db, _today=datetime.date(2026, 8, 24)
    )

    assert result["calibrated"] >= 1
    assert target.model_config.get("p_rise_calibration") is not None
    assert "curve" in target.model_config["p_rise_calibration"]


def test_calibration_skips_with_too_few_decisions():
    """With <20 decisions, target is skipped."""
    from app.services.forecasting.ops.p_rise_calibration import run_weekly_p_rise_calibration

    db = MagicMock()
    target = MagicMock()
    target.name = "PE_PP"
    target.product_key = "PE_PP"
    target.model_config = {}

    decisions = []
    for i in range(5):
        d = MagicMock()
        d.predicted_p_rise = 0.5
        d.actual_price_t = 100.0
        d.actual_price_th = 105.0
        decisions.append(d)

    def mock_query(model):
        q = MagicMock()
        name = getattr(model, "__name__", "")
        if name == "ForecastTarget":
            q.filter.return_value.all.return_value = [target]
        else:
            q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = decisions
        return q

    db.query.side_effect = mock_query

    result = run_weekly_p_rise_calibration(
        db, _today=datetime.date(2026, 8, 24)
    )

    assert result["calibrated"] == 0
    assert result["skipped"] >= 1
