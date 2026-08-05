"""Test MASE, CRPS, and interval coverage metrics in backtest."""
import numpy as np
import pandas as pd

from app.services.forecasting.backtest import evaluate
from app.services.forecasting.models.ets import ETSModel
from app.services.forecasting.models.naive import NaiveLast, SeasonalNaive


def _make_series(n=120, seed=42):
    """Generate a deterministic daily series with seasonality + trend."""
    rng = np.random.RandomState(seed)
    t = np.arange(n, dtype=float)
    seasonal = 10 * np.sin(2 * np.pi * t / 7)
    trend = 0.05 * t
    noise = rng.randn(n) * 2
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.Series(100 + trend + seasonal + noise, index=idx)


def test_backtest_result_has_mase_field():
    """BacktestResult must include per_model_mase."""
    y = _make_series()
    models = {"ets": ETSModel()}
    result = evaluate(y, models, seasonal_period=7)
    assert hasattr(result, "per_model_mase")
    assert isinstance(result.per_model_mase, dict)


def test_mase_is_finite_for_working_model():
    """A working model (ETS) should have a finite or zero MASE value."""
    y = _make_series()
    models = {"ets": ETSModel()}
    result = evaluate(y, models, seasonal_period=7)
    ets_mase = result.per_model_mase.get("ets", float("inf"))
    assert np.isfinite(ets_mase), f"ETS MASE should be finite, got {ets_mase}"


def test_backtest_result_has_crps_field():
    """BacktestResult must include per_model_crps."""
    y = _make_series()
    models = {"ets": ETSModel()}
    result = evaluate(y, models, seasonal_period=7)
    assert hasattr(result, "per_model_crps")
    assert isinstance(result.per_model_crps, dict)


def test_crps_none_for_non_probabilistic():
    """Non-probabilistic models (ETS) should have CRPS = None."""
    y = _make_series()
    models = {"ets": ETSModel()}
    result = evaluate(y, models, seasonal_period=7)
    # ETS doesn't implement forecast_quantiles -> CRPS should be None
    assert result.per_model_crps.get("ets") is None


def test_backtest_result_has_interval_fields():
    """BacktestResult must include interval_coverage and interval_width."""
    y = _make_series()
    models = {"ets": ETSModel()}
    result = evaluate(y, models, seasonal_period=7)
    assert hasattr(result, "interval_coverage")
    assert hasattr(result, "interval_width")


def test_interval_none_for_non_probabilistic():
    """Non-probabilistic models should have None interval coverage/width."""
    y = _make_series()
    models = {"ets": ETSModel()}
    result = evaluate(y, models, seasonal_period=7)
    assert result.interval_coverage.get("ets") is None
    assert result.interval_width.get("ets") is None


def test_mase_dict_contains_all_models():
    """MASE dict should contain entries for all evaluated models."""
    y = _make_series()
    models = {"ets": ETSModel()}
    result = evaluate(y, models, seasonal_period=7)
    # ETS + seasonal_naive (auto-added benchmark)
    assert "ets" in result.per_model_mase
    assert "seasonal_naive" in result.per_model_mase


def test_crps_dict_contains_all_models():
    """CRPS dict should contain entries for all evaluated models."""
    y = _make_series()
    models = {"ets": ETSModel()}
    result = evaluate(y, models, seasonal_period=7)
    assert "ets" in result.per_model_crps
    assert "seasonal_naive" in result.per_model_crps
