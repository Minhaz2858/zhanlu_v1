"""Test MoiraiModel wrapper — uses mocked model (no model download)."""
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


def _make_exog(n=100):
    return pd.DataFrame(
        {"feedstock": np.random.randn(n) * 10 + 200},
        index=pd.date_range("2025-01-01", periods=n, freq="D"),
    )


def test_moirai_is_forecast_model():
    """MoiraiModel must be a ForecastModel subclass."""
    try:
        from app.services.forecasting.models.moirai import MoiraiModel
    except ImportError:
        pytest.skip("moirai module not importable (torch missing)")
    assert issubclass(MoiraiModel, ForecastModel)


def test_moirai_import_error_skipped():
    """Deferred loading: __init__ succeeds, _load_model raises ImportError."""
    from app.services.forecasting.models.moirai import MoiraiModel

    # __init__ should NOT raise (model loading is deferred)
    model = MoiraiModel()
    assert model._model is None  # not loaded

    # But _load_model should raise ImportError when torch/uni2ts missing
    with patch.dict("sys.modules", {"torch": None, "uni2ts": None}):
        with pytest.raises(ImportError):
            model._load_model()


def test_moirai_fit_forecast_mocked():
    """fit() + forecast() return pd.Series of length h (mocked model)."""
    from app.services.forecasting.models.moirai import MoiraiModel

    fake_samples = np.random.randn(5, 7).astype(np.float32) * 10 + 100
    mock_model = MagicMock()
    mock_result = MagicMock()
    mock_result.samples = MagicMock(
        __getitem__=lambda s, k: fake_samples,
        numpy=lambda: fake_samples,
    )
    mock_model.return_value = mock_result

    with patch.object(MoiraiModel, "_load_model", return_value=mock_model):
        m = MoiraiModel()
        m.fit(_make_series(100), seasonal_period=7, exog=_make_exog(100))
        pred = m.forecast(7)
        assert isinstance(pred, pd.Series)
        assert len(pred) == 7


def test_moirai_forecast_quantiles_mocked():
    """forecast_quantiles() returns dict of quantile -> pd.Series."""
    from app.services.forecasting.models.moirai import MoiraiModel

    fake_samples = np.random.randn(5, 7).astype(np.float32) * 10 + 100
    mock_model = MagicMock()
    mock_result = MagicMock()
    mock_result.samples = MagicMock(
        __getitem__=lambda s, k: fake_samples,
        numpy=lambda: fake_samples,
    )
    mock_model.return_value = mock_result

    with patch.object(MoiraiModel, "_load_model", return_value=mock_model):
        m = MoiraiModel()
        m.fit(_make_series(100), seasonal_period=7)
        qfc = m.forecast_quantiles(7, quantiles=[0.1, 0.5, 0.9])
        assert qfc is not None
        assert len(qfc[0.5]) == 7


def test_moirai_name_and_min_history():
    """Model name should be 'moirai' and min_history >= 60."""
    from app.services.forecasting.models.moirai import MoiraiModel

    assert MoiraiModel.name == "moirai"
    assert MoiraiModel.min_history >= 60


def test_moirai_uses_exog_flag():
    """uses_exog should be False initially, True after fit with exog."""
    from app.services.forecasting.models.moirai import MoiraiModel

    mock_model = MagicMock()
    mock_result = MagicMock()
    mock_result.samples = MagicMock(
        __getitem__=lambda s, k: np.random.randn(5, 7).astype(np.float32),
        numpy=lambda: np.random.randn(5, 7).astype(np.float32),
    )
    mock_model.return_value = mock_result

    with patch.object(MoiraiModel, "_load_model", return_value=mock_model):
        m = MoiraiModel()
        assert m.uses_exog is False
        m.fit(_make_series(100), seasonal_period=7, exog=_make_exog(100))
        assert m.uses_exog is True
