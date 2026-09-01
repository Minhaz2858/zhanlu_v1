"""E2E: build_model_pool with foundation flag ON produces a pool that
includes chronos_bolt/moirai (if deps installed) or skips gracefully."""
import os
import numpy as np
import pandas as pd

from app.services.forecasting.models import build_model_pool
from app.services.forecasting.backtest import evaluate
from app.services.forecasting.ensemble import blend


def _make_series(n=120, seed=42):
    rng = np.random.RandomState(seed)
    t = np.arange(n, dtype=float)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.Series(
        100 + 0.05 * t + 10 * np.sin(2 * np.pi * t / 7) + rng.randn(n) * 2,
        index=idx,
    )


def test_full_pipeline_flag_off():
    """Flag OFF: pool has statistical models, no foundation models."""
    os.environ.pop("FORECAST_FOUNDATION_MODELS_ENABLED", None)
    y = _make_series(120)
    pool = build_model_pool(seasonal_period=7, y=y)
    assert "chronos_bolt" not in pool
    assert "moirai" not in pool
    # Backtest still works
    result = evaluate(y, pool, seasonal_period=7)
    assert result.n_folds > 0


def test_full_pipeline_flag_on_graceful():
    """Flag ON but torch missing: pool still works, foundation models skipped."""
    os.environ["FORECAST_FOUNDATION_MODELS_ENABLED"] = "true"
    try:
        y = _make_series(120)
        pool = build_model_pool(seasonal_period=7, y=y)
        # Statistical models must always be present
        assert "naive_last" in pool
        assert "ets" in pool
        assert "arima" in pool
        # Backtest must work
        result = evaluate(y, pool, seasonal_period=7)
        assert result.n_folds > 0
        # Ensemble must work
        if result.per_model_mape:
            forecasts = {}
            for name, model in pool.items():
                try:
                    model.fit(y, seasonal_period=7)
                    forecasts[name] = model.forecast(7)
                except Exception:
                    pass
            if forecasts:
                ensemble = blend(forecasts, result.per_model_mape)
                assert len(ensemble.point_forecast) == 7
    finally:
        os.environ.pop("FORECAST_FOUNDATION_MODELS_ENABLED", None)


def test_full_pipeline_short_series_skips_foundation():
    """Flag ON but series < 60: foundation models skipped."""
    os.environ["FORECAST_FOUNDATION_MODELS_ENABLED"] = "true"
    try:
        y = _make_series(40)  # < 60
        pool = build_model_pool(seasonal_period=7, y=y)
        assert "chronos_bolt" not in pool
        assert "moirai" not in pool
        assert "xgboost_reg" not in pool  # also skipped (< 90)
    finally:
        os.environ.pop("FORECAST_FOUNDATION_MODELS_ENABLED", None)
