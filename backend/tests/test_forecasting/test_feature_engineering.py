"""
Wave 5 — Feature Engineering Expansion tests.

Coverage:
- technical_indicators: RSI, MACD, Bollinger Bands
- fourier_features: sin/cos harmonics
- feature_builder integration: tech/fourier columns in FeatureMatrix
"""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timedelta


# ================================================================
# Test data helpers
# ================================================================

def make_test_series(n: int = 180, seed: int = 42) -> pd.Series:
    """Generate a realistic price-like time series."""
    rng = np.random.default_rng(seed)
    dr = pd.date_range("2025-01-01", periods=n, freq="D")
    trend = np.linspace(100, 120, n)
    season = 3 * np.sin(2 * np.pi * np.arange(n) / 7)  # weekly
    noise = rng.normal(0, 1.5, n)
    return pd.Series(trend + season + noise, index=dr, name="price")


# ================================================================
# Technical Indicators
# ================================================================

class TestTechnicalIndicators:
    """Test RSI, MACD, Bollinger Bands computation."""

    def test_rsi_computation(self):
        from app.services.forecasting.features.technical_indicators import compute_rsi

        y = make_test_series(60)
        rsi = compute_rsi(y, period=14)
        assert isinstance(rsi, pd.Series)
        assert len(rsi) == len(y)
        assert rsi.min() >= 0
        assert rsi.max() <= 100
        # Early values filled with neutral 50.0 (min_periods=1 + fillna)
        assert rsi.iloc[0] == 50.0
        assert isinstance(rsi.iloc[20], (float, np.floating))

    def test_rsi_short_series(self):
        from app.services.forecasting.features.technical_indicators import compute_rsi

        y = make_test_series(10)
        rsi = compute_rsi(y, period=14)
        assert isinstance(rsi, pd.Series)
        assert len(rsi) == len(y)
        # Short series: all values default to 50.0 (fillna)
        assert (rsi == 50.0).all() or rsi.isna().all()

    def test_macd_computation(self):
        from app.services.forecasting.features.technical_indicators import compute_macd

        y = make_test_series(100)
        macd, signal, hist = compute_macd(y)
        assert isinstance(macd, pd.Series)
        assert len(macd) == len(y)
        # All values should be defined (fillna with 0.0)
        assert macd.notna().all()
        assert isinstance(macd.iloc[50], (float, np.floating))

    def test_bollinger_computation(self):
        from app.services.forecasting.features.technical_indicators import compute_bollinger

        y = make_test_series(60)
        middle, upper, lower, bw = compute_bollinger(y, window=20, n_std=2)
        assert isinstance(middle, pd.Series)
        assert len(middle) == len(y)
        # First (window-1) values are NaN; check non-NaN portion
        valid = upper.notna() & lower.notna()
        assert (upper[valid] >= lower[valid]).all()
        # Bandwidth should be positive (skip NaN)
        valid_bw = bw.dropna()
        assert len(valid_bw) > 0 and (valid_bw > 0).all()

    def test_add_technical_features(self):
        from app.services.forecasting.features.technical_indicators import add_technical_features

        y = make_test_series(100)
        result = add_technical_features(y)
        assert isinstance(result, pd.DataFrame)
        assert "rsi" in result.columns
        assert "macd" in result.columns
        assert "bb_bw" in result.columns
        assert len(result) == len(y)


# ================================================================
# Fourier Features
# ================================================================

class TestFourierFeatures:
    """Test Fourier sin/cos harmonic decomposition."""

    def test_compute_fourier_terms(self):
        from app.services.forecasting.features.fourier_features import compute_fourier_terms

        y = make_test_series(50)
        fourier = compute_fourier_terms(y, n_harmonics=3, target_period=7)
        assert isinstance(fourier, pd.DataFrame)
        assert len(fourier) == len(y)
        # 3 harmonics × 2 (sin + cos) = 6 columns
        assert fourier.shape[1] == 6
        assert "fourier_sin_1" in fourier.columns
        assert "fourier_cos_3" in fourier.columns

    def test_fourier_range(self):
        from app.services.forecasting.features.fourier_features import compute_fourier_terms

        y = make_test_series(100)
        fourier = compute_fourier_terms(y, n_harmonics=2, target_period=14)
        # All values in [-1, 1]
        assert (fourier >= -1.0001).all().all()
        assert (fourier <= 1.0001).all().all()

    def test_fourier_periodicity(self):
        """Verify Fourier features have correct period."""
        from app.services.forecasting.features.fourier_features import compute_fourier_terms
        import numpy as np

        y = make_test_series(21)  # exactly 3 weeks
        fourier = compute_fourier_terms(y, n_harmonics=1, target_period=7)
        sin1 = fourier["fourier_sin_1"].values
        # After 7 days, sin should repeat
        assert np.abs(sin1[0] - sin1[7]) < 1e-10
        assert np.abs(sin1[1] - sin1[8]) < 1e-10

    def test_add_fourier_features(self):
        from app.services.forecasting.features.fourier_features import add_fourier_features

        y = make_test_series(100)
        result = add_fourier_features(y, weekly_harmonics=2)
        assert isinstance(result, pd.DataFrame)
        assert "fourier_sin_1" in result.columns
        assert "fourier_cos_2" in result.columns
        assert len(result) == len(y)


# ================================================================
# Feature Builder Integration — Tech Indicators + Fourier
# ================================================================

class TestFeatureBuilderWave5:
    """Test that build_features includes Wave 5 columns when flags are ON."""

    def _make_spec(self):
        from app.services.forecasting.features.feature_registry import FeatureSpec

        return FeatureSpec(
            product_key="test_product",
            feedstock_keys=["naphtha"],
            feedstock_lags=[1, 2, 3, 4, 5, 6, 7],
            use_fx=True,
            use_event_flags=True,
        )

    def _make_mock_loaders(self, n: int = 250):
        from unittest.mock import MagicMock

        feed = MagicMock()
        feed.get_series.return_value = make_test_series(n)
        fx = MagicMock()
        fx.get_series.return_value = make_test_series(n) * 7.2
        event = MagicMock()
        event.get_today_event_flags.return_value = {"has_event": False, "event_type": 0}
        event.get_historical_event_flags.return_value = pd.DataFrame(
            {"has_event": False, "event_type": 0}, index=pd.date_range("2025-01-01", periods=n, freq="D")
        )
        return feed, fx, event

    def test_tech_indicators_integration(self):
        """When tech_indicators_enabled=True, rsi/macd/bb_bandwidth columns exist."""
        from app.services.forecasting.features.feature_builder import build_features

        y = make_test_series(180)
        feed, fx, event = self._make_mock_loaders()
        fm = build_features(
            "test_product", y, self._make_spec(),
            feed, fx, event, horizon=7,
            cascade_forecasts=None,
            tech_indicators_enabled=True,
            fourier_enabled=False,
        )
        assert fm.feature_names is not None
        assert "rsi_14" in fm.feature_names
        assert "macd" in fm.feature_names
        assert "bb_bandwidth" in fm.feature_names
        assert fm.X_train is not None
        # Verify values are within reasonable range
        rsi_col = fm.feature_names.index("rsi_14")
        rsi_vals = fm.X_train.iloc[:, rsi_col]
        assert rsi_vals.min() >= 0
        assert rsi_vals.max() <= 100

    def test_fourier_integration(self):
        """When fourier_enabled=True, fourier sin/cos columns exist."""
        from app.services.forecasting.features.feature_builder import build_features

        y = make_test_series(180)
        feed, fx, event = self._make_mock_loaders()
        fm = build_features(
            "test_product", y, self._make_spec(),
            feed, fx, event, horizon=7,
            cascade_forecasts=None,
            tech_indicators_enabled=False,
            fourier_enabled=True,
        )
        assert fm.feature_names is not None
        assert "fourier_sin_1" in fm.feature_names
        assert "fourier_cos_1" in fm.feature_names
        assert "fourier_sin_2" in fm.feature_names
        assert "fourier_cos_2" in fm.feature_names
        assert "fourier_sin_3" in fm.feature_names
        assert "fourier_cos_3" in fm.feature_names

    def test_tech_and_fourier_both_on(self):
        """When both enabled, all feature columns present."""
        from app.services.forecasting.features.feature_builder import build_features

        y = make_test_series(180)
        feed, fx, event = self._make_mock_loaders()
        fm = build_features(
            "test_product", y, self._make_spec(),
            feed, fx, event, horizon=7,
            cascade_forecasts=None,
            tech_indicators_enabled=True,
            fourier_enabled=True,
        )
        assert fm.feature_names is not None
        for col in ["rsi_14", "macd", "bb_bandwidth",
                     "fourier_sin_1", "fourier_cos_1",
                     "fourier_sin_2", "fourier_cos_2",
                     "fourier_sin_3", "fourier_cos_3"]:
            assert col in fm.feature_names, f"Missing column: {col}"

    def test_disabled_no_regression(self):
        """When all Wave 5 flags OFF, no new columns (backward compat)."""
        from app.services.forecasting.features.feature_builder import build_features

        y = make_test_series(180)
        feed, fx, event = self._make_mock_loaders()
        fm = build_features(
            "test_product", y, self._make_spec(),
            feed, fx, event, horizon=7,
            cascade_forecasts=None,
            tech_indicators_enabled=False,
            fourier_enabled=False,
        )
        assert fm.feature_names is not None
        assert "rsi_14" not in fm.feature_names
        assert "fourier_sin_1" not in fm.feature_names

    def test_future_rows_have_tech_indicators(self):
        """Future rows (cascade mode) should have tech indicators carried forward."""
        from app.services.forecasting.features.feature_builder import build_features

        y = make_test_series(180)
        feed, fx, event = self._make_mock_loaders()
        # Enable cascade to trigger X_future construction
        cascade = {"naphtha": [100.0 + i for i in range(7)]}
        fm = build_features(
            "test_product", y, self._make_spec(),
            feed, fx, event, horizon=7,
            cascade_forecasts=cascade,
            tech_indicators_enabled=True,
            fourier_enabled=False,
        )
        assert fm.X_future is not None
        assert fm.X_future.shape[0] == 7  # horizon rows
        rsi_col = fm.feature_names.index("rsi_14")
        future_rsi = fm.X_future.iloc[:, rsi_col]
        # All RSI values carried forward from last training date
        assert np.allclose(future_rsi, future_rsi.iloc[0])
        assert 0 <= future_rsi.iloc[0] <= 100

    def test_future_rows_have_fourier(self):
        """Future rows (cascade mode) should have Fourier features positionally extended."""
        from app.services.forecasting.features.feature_builder import build_features

        y = make_test_series(180)
        feed, fx, event = self._make_mock_loaders()
        cascade = {"naphtha": [100.0 + i for i in range(7)]}
        fm = build_features(
            "test_product", y, self._make_spec(),
            feed, fx, event, horizon=7,
            cascade_forecasts=cascade,
            tech_indicators_enabled=False,
            fourier_enabled=True,
        )
        assert fm.X_future is not None
        sin1_col = fm.feature_names.index("fourier_sin_1")
        future_sin1 = fm.X_future.iloc[:, sin1_col]
        assert np.all(np.abs(future_sin1) <= 1.0)

    def test_short_series_tech_skipped(self):
        """Very short series (<30 pts): tech columns present but filled with neutral values."""
        from app.services.forecasting.features.feature_builder import build_features

        y = make_test_series(15)  # Too short
        feed, fx, event = self._make_mock_loaders()
        fm = build_features(
            "test_product", y, self._make_spec(),
            feed, fx, event, horizon=7,
            cascade_forecasts=None,
            tech_indicators_enabled=True,
            fourier_enabled=False,
        )
        assert fm.feature_names is not None
        # Columns exist but filled with neutral defaults (50.0, 0.0, 0.0)
        assert fm.X_train is not None
        rsi_col = fm.feature_names.index("rsi_14")
        assert (fm.X_train.iloc[:, rsi_col] == 50.0).all()

    def test_external_only_matrix_wave5(self):
        """
        When no feedstock keys but Wave 5 enabled, _build_external_only_matrix
        should also include tech/fourier columns.
        """
        from app.services.forecasting.features.feature_builder import build_features
        from app.services.forecasting.features.feature_registry import FeatureSpec

        spec = FeatureSpec(
            product_key="test_product",
            feedstock_keys=[],  # No feedstock → external-only path
            feedstock_lags=[1, 2, 3],
            use_fx=True,
            use_event_flags=True,
        )

        y = make_test_series(100)
        feed, fx, event = self._make_mock_loaders()

        # With volume enabled (needed to avoid early return)
        import os
        os.environ["FORECAST_ERP_VOLUME_EXOG_ENABLED"] = "true"

        fm = build_features(
            "test_product", y, spec,
            feed, fx, event, horizon=7,
            cascade_forecasts=None,
            volume_df=pd.DataFrame({
                "volume": np.random.default_rng(1).normal(500, 50, len(y)),
            }, index=y.index),
            tech_indicators_enabled=True,
            fourier_enabled=True,
        )
        assert fm.feature_names is not None
        # tech + fourier should still be present
        assert "rsi_14" in fm.feature_names
        assert "fourier_sin_1" in fm.feature_names
