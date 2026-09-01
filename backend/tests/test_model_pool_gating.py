"""Tests for XGBoost min-rows gating in build_model_pool (Phase C Task C3)."""
from __future__ import annotations

import pandas as pd

from app.services.forecasting.models import build_model_pool


def test_xgboost_skipped_on_short_series():
    """Series with < 90 rows should not include XGBoost models."""
    y = pd.Series(range(70), dtype=float)
    pool = build_model_pool(y=y, seasonal_period=7)
    assert "xgboost_reg" not in pool
    assert "xgboost_exog" not in pool
    # Statistical models remain
    assert "arima" in pool
    assert "naive_last" in pool


def test_xgboost_present_on_long_series():
    """Series with >= 90 rows should include XGBoost models."""
    y = pd.Series(range(120), dtype=float)
    pool = build_model_pool(y=y, seasonal_period=7)
    assert "xgboost_reg" in pool
    assert "xgboost_exog" in pool


def test_no_y_backward_compat():
    """Without y param, all models included (old behavior)."""
    pool = build_model_pool(seasonal_period=7)
    assert "arima" in pool
    assert "xgboost_reg" in pool


def test_threshold_exactly_90():
    """Exactly 90 rows → XGBoost included (boundary)."""
    y = pd.Series(range(90), dtype=float)
    pool = build_model_pool(y=y, seasonal_period=7)
    assert "xgboost_reg" in pool
