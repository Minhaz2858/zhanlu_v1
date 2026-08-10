"""Test ChronosBoltModel wrapper — uses mocked pipeline (no model download)."""
import numpy as np
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

from app.services.forecasting.models.base import ForecastModel


def _make_series(n=100, seed=42):
    rng = np.random.RandomState(seed)
    return pd.Series(
        100 + rng.randn(n) * 5,
        index=pd.date_range("2025-01-01", periods=n, freq="D"),
    )


def test_chronos_is_forecast_model():
    """ChronosBoltModel must be a ForecastModel subclass."""
    try:
        from app.services.forecasting.models.chronos_bolt import ChronosBoltModel
    except ImportError:
        pytest.skip("chronos_bolt module not importable (torch missing)")
    assert issubclass(ChronosBoltModel, ForecastModel)


def test_chronos_import_error_skipped():
    """Deferred loading: __init__ succeeds, _load_pipeline raises ImportError."""
    from app.services.forecasting.models.chronos_bolt import ChronosBoltModel

    # __init__ should NOT raise (pipeline loading is deferred)
    model = ChronosBoltModel()
    assert model._pipeline is None  # not loaded

    # But _load_pipeline should raise ImportError when torch/chronos missing
    with patch.dict("sys.modules", {"torch": None, "chronos": None}):
        with pytest.raises(ImportError):
            model._load_pipeline()


def test_chronos_fit_forecast_mocked():
    """fit() + forecast() return pd.Series of length h (mocked pipeline)."""
    from app.services.forecasting.models.chronos_bolt import ChronosBoltModel

    fake_forecast = np.random.randn(20, 7).astype(np.float32) * 10 + 100
    mock_pipeline = MagicMock()
    mock_pipeline.predict.return_value = MagicMock(numpy=lambda: fake_forecast)

    with patch.object(ChronosBoltModel, "_load_pipeline", return_value=mock_pipeline):
        model = ChronosBoltModel(model_name="amazon/chronos-bolt-mini")
        y = _make_series(100)
        model.fit(y, seasonal_period=7)
        pred = model.forecast(7)
        assert isinstance(pred, pd.Series)
        assert len(pred) == 7
        expected_median = np.median(fake_forecast, axis=0)
        np.testing.assert_allclose(pred.values, expected_median, rtol=1e-5)


def test_chronos_forecast_quantiles_mocked():
    """forecast_quantiles() returns dict of quantile -> pd.Series."""
    from app.services.forecasting.models.chronos_bolt import ChronosBoltModel

    fake_forecast = np.random.randn(20, 7).astype(np.float32) * 10 + 100
    mock_pipeline = MagicMock()
    mock_pipeline.predict.return_value = MagicMock(numpy=lambda: fake_forecast)

    with patch.object(ChronosBoltModel, "_load_pipeline", return_value=mock_pipeline):
        model = ChronosBoltModel()
        y = _make_series(100)
        model.fit(y, seasonal_period=7)
        qfc = model.forecast_quantiles(7, quantiles=[0.1, 0.5, 0.9])
        assert qfc is not None
        assert 0.1 in qfc and 0.5 in qfc and 0.9 in qfc
        assert len(qfc[0.5]) == 7
        # q10 <= q50 <= q90
        assert np.all(qfc[0.1].values <= qfc[0.5].values + 1e-5)
        assert np.all(qfc[0.5].values <= qfc[0.9].values + 1e-5)


def test_chronos_min_history():
    """ChronosBoltModel should declare min_history for build_model_pool gating."""
    from app.services.forecasting.models.chronos_bolt import ChronosBoltModel

    assert ChronosBoltModel.min_history >= 30


def test_chronos_name():
    """Model name should be 'chronos_bolt'."""
    from app.services.forecasting.models.chronos_bolt import ChronosBoltModel

    with patch.object(ChronosBoltModel, "_load_pipeline", return_value=MagicMock()):
        m = ChronosBoltModel()
        assert m.name == "chronos_bolt"
