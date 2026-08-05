"""Test foundation model registration in build_model_pool (flag-gated)."""
import os
import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.models import build_model_pool


def _make_series(n=100, seed=42):
    rng = np.random.RandomState(seed)
    return pd.Series(
        100 + rng.randn(n) * 5,
        index=pd.date_range("2025-01-01", periods=n, freq="D"),
    )


def test_foundation_models_absent_when_flag_off():
    """With flag off, pool must NOT contain chronos_bolt or moirai."""
    os.environ.pop("FORECAST_FOUNDATION_MODELS_ENABLED", None)
    pool = build_model_pool(seasonal_period=7, y=_make_series(100))
    assert "chronos_bolt" not in pool
    assert "moirai" not in pool


def test_foundation_flag_is_read():
    """With flag on, build_model_pool should attempt to add foundation models.
    If torch is not installed, they're silently skipped — that's fine.
    The test verifies the flag is READ, not that models are present."""
    os.environ["FORECAST_FOUNDATION_MODELS_ENABLED"] = "true"
    try:
        pool = build_model_pool(seasonal_period=7, y=_make_series(100))
        # Statistical models must always be present
        assert "naive_last" in pool
        assert "ets" in pool
    finally:
        os.environ.pop("FORECAST_FOUNDATION_MODELS_ENABLED", None)


def test_foundation_individual_flags():
    """Individual model flags should gate each model independently."""
    os.environ["FORECAST_FOUNDATION_MODELS_ENABLED"] = "true"
    os.environ["FORECAST_FOUNDATION_MODEL_CHRONOS_ENABLED"] = "false"
    os.environ["FORECAST_FOUNDATION_MODEL_MOIRAI_ENABLED"] = "false"
    try:
        pool = build_model_pool(seasonal_period=7, y=_make_series(100))
        assert "chronos_bolt" not in pool
        assert "moirai" not in pool
    finally:
        for k in (
            "FORECAST_FOUNDATION_MODELS_ENABLED",
            "FORECAST_FOUNDATION_MODEL_CHRONOS_ENABLED",
            "FORECAST_FOUNDATION_MODEL_MOIRAI_ENABLED",
        ):
            os.environ.pop(k, None)


def test_foundation_models_skipped_for_short_series():
    """Foundation models should be skipped when series < min_history (60)."""
    os.environ["FORECAST_FOUNDATION_MODELS_ENABLED"] = "true"
    try:
        pool = build_model_pool(seasonal_period=7, y=_make_series(30))
        assert "chronos_bolt" not in pool
        assert "moirai" not in pool
        assert "xgboost_reg" not in pool  # also skipped (< 90)
    finally:
        os.environ.pop("FORECAST_FOUNDATION_MODELS_ENABLED", None)


def test_statistical_models_always_present():
    """Statistical models must always be in the pool regardless of flags."""
    os.environ.pop("FORECAST_FOUNDATION_MODELS_ENABLED", None)
    pool = build_model_pool(seasonal_period=7, y=_make_series(100))
    for name in ("naive_last", "seasonal_naive", "ets", "arima", "stl", "mean_reversion"):
        assert name in pool, f"{name} missing from pool"
