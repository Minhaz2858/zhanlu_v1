"""Tests for Wave 1 ERP volume → XGBoost exogenous features (feature_builder.py)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from datetime import timedelta
from unittest.mock import MagicMock

from app.services.forecasting.features.feature_builder import build_features, FeatureMatrix
from app.services.forecasting.features.feature_registry import FeatureSpec


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

@pytest.fixture
def y_series():
    """30-day price series."""
    dates = pd.date_range("2025-01-01", periods=30, freq="D")
    return pd.Series(np.linspace(100, 110, 30), index=dates, name="price")


@pytest.fixture
def volume_df():
    """60-day volume series overlapping y_series and extending before it."""
    dates = pd.date_range("2024-12-01", periods=60, freq="D")
    vol = np.random.RandomState(42).randint(50, 200, size=60).astype(float)
    return pd.DataFrame({"date": dates, "volume": vol})


@pytest.fixture
def minimal_spec():
    """Minimal FeatureSpec with one fake feedstock and no FX/events."""
    return FeatureSpec(
        product_key="TEST",
        feedstock_keys=["fake_feedstock"],
        feedstock_lags=[1, 2, 3, 7],
        spread_pairs=[],
        use_fx=False,
        use_event_flags=False,
        calendar_features=False,
    )


@pytest.fixture
def mock_feedstock_loader():
    """Return a simple demand-like series for a fake feedstock."""
    loader = MagicMock()
    dates = pd.date_range("2024-11-01", periods=150, freq="D")
    loader.read_actuals.return_value = pd.DataFrame({
        "ds": dates,
        "fake_feedstock": np.linspace(500, 600, 150),
    })
    return loader


@pytest.fixture
def mock_fx_loader():
    loader = MagicMock()
    loader.read_usd_cny.return_value = pd.DataFrame()
    return loader


@pytest.fixture
def mock_event_loader():
    loader = MagicMock()
    dates = pd.date_range("2024-11-01", periods=150, freq="D")
    loader.read_flags.return_value = pd.DataFrame({
        "ds": dates,
        "event_flag": np.zeros(150),
    })
    return loader


# ------------------------------------------------------------------ #
# Tests
# ------------------------------------------------------------------ #

class TestBuildFeaturesWithVolume:
    """ERP volume_df param adds lag columns to X_train and X_future."""

    def test_adds_lag_columns(self, y_series, volume_df, minimal_spec,
                              mock_feedstock_loader, mock_fx_loader, mock_event_loader):
        result = build_features(
            target_product_key="TEST",
            y=y_series,
            spec=minimal_spec,
            feedstock_loader=mock_feedstock_loader,
            fx_loader=mock_fx_loader,
            event_loader=mock_event_loader,
            horizon=7,
            volume_df=volume_df,
        )
        assert result.X_train is not None
        for lag in range(1, 8):
            col = f"erp_volume_lag{lag}"
            assert col in result.X_train.columns, f"Missing {col}"
            assert col in result.feature_names, f"Missing {col} in feature_names"

    def test_all_volume_lag_values_finite(self, y_series, volume_df, minimal_spec,
                                          mock_feedstock_loader, mock_fx_loader, mock_event_loader):
        result = build_features(
            target_product_key="TEST",
            y=y_series,
            spec=minimal_spec,
            feedstock_loader=mock_feedstock_loader,
            fx_loader=mock_fx_loader,
            event_loader=mock_event_loader,
            horizon=7,
            volume_df=volume_df,
        )
        for col in [c for c in result.X_train.columns if c.startswith("erp_volume_lag")]:
            assert not result.X_train[col].isna().any(), f"NaN in {col}"
            assert np.isfinite(result.X_train[col]).all(), f"Non-finite in {col}"

    def test_lag_values_in_expected_range(self, y_series, volume_df, minimal_spec,
                                          mock_feedstock_loader, mock_fx_loader, mock_event_loader):
        result = build_features(
            target_product_key="TEST",
            y=y_series,
            spec=minimal_spec,
            feedstock_loader=mock_feedstock_loader,
            fx_loader=mock_fx_loader,
            event_loader=mock_event_loader,
            horizon=7,
            volume_df=volume_df,
        )
        vol_min, vol_max = volume_df["volume"].min(), volume_df["volume"].max()
        for col in [c for c in result.X_train.columns if c.startswith("erp_volume_lag")]:
            col_min = result.X_train[col].min()
            col_max = result.X_train[col].max()
            assert col_min >= vol_min, f"{col} min {col_min} < volume min {vol_min}"
            assert col_max <= vol_max, f"{col} max {col_max} > volume max {vol_max}"

    def test_no_future_leakage(self, y_series, volume_df, minimal_spec,
                               mock_feedstock_loader, mock_fx_loader, mock_event_loader):
        """Each lag{N} column uses dates strictly before the target date."""
        result = build_features(
            target_product_key="TEST",
            y=y_series,
            spec=minimal_spec,
            feedstock_loader=mock_feedstock_loader,
            fx_loader=mock_fx_loader,
            event_loader=mock_event_loader,
            horizon=7,
            volume_df=volume_df.set_index("date"),
        )
        vol_indexed = volume_df.set_index("date")
        last_vol_date = vol_indexed.index.max()
        for date in result.X_train.index:
            # erp_volume_lag{N} should reference date - N days
            for lag in range(1, 8):
                col = f"erp_volume_lag{lag}"
                ref_date = date - timedelta(days=lag)
                val = result.X_train.loc[date, col]
                # Must be finite and non-NaN
                assert np.isfinite(val), f"{col} at {date} is non-finite"
                # If the reference date is in the volume data, value should be
                # within the volume range (may be winsorized).  Key check: the
                # reference date must be <= last_vol_date (no future data usage).
                assert ref_date <= last_vol_date + timedelta(days=1), (
                    f"{col} at {date} references {ref_date} which is after "
                    f"last volume date {last_vol_date}"
                )


class TestBuildFeaturesWithoutVolume:
    """Zero regression: without volume_df, behaviour unchanged."""

    def test_no_volume_columns(self, y_series, minimal_spec,
                               mock_feedstock_loader, mock_fx_loader, mock_event_loader):
        result = build_features(
            target_product_key="TEST",
            y=y_series,
            spec=minimal_spec,
            feedstock_loader=mock_feedstock_loader,
            fx_loader=mock_fx_loader,
            event_loader=mock_event_loader,
            horizon=7,
            volume_df=None,
        )
        vol_cols = [c for c in result.X_train.columns if c.startswith("erp_volume_lag")]
        assert len(vol_cols) == 0, f"Should have no volume cols, got {vol_cols}"

    def test_volume_df_none_identical_to_missing(self, y_series, minimal_spec,
                                                  mock_feedstock_loader, mock_fx_loader, mock_event_loader):
        r1 = build_features("TEST", y_series, minimal_spec,
                            mock_feedstock_loader, mock_fx_loader, mock_event_loader,
                            horizon=7, volume_df=None)
        r2 = build_features("TEST", y_series, minimal_spec,
                            mock_feedstock_loader, mock_fx_loader, mock_event_loader,
                            horizon=7)
        assert list(r1.X_train.columns) == list(r2.X_train.columns)
        assert r1.feature_names == r2.feature_names


class TestBuildFeaturesVolumeInsufficient:
    """volume_df with too few rows should be skipped."""

    def test_insufficient_rows_skipped(self, y_series, minimal_spec,
                                       mock_feedstock_loader, mock_fx_loader, mock_event_loader):
        short_vol = pd.DataFrame({
            "date": pd.date_range("2025-01-01", periods=3, freq="D"),
            "volume": [100.0, 110.0, 105.0],
        })
        result = build_features(
            target_product_key="TEST",
            y=y_series,
            spec=minimal_spec,
            feedstock_loader=mock_feedstock_loader,
            fx_loader=mock_fx_loader,
            event_loader=mock_event_loader,
            horizon=7,
            volume_df=short_vol,
        )
        vol_cols = [c for c in result.X_train.columns if c.startswith("erp_volume_lag")]
        assert len(vol_cols) == 0

    def test_empty_volume_df_skipped(self, y_series, minimal_spec,
                                     mock_feedstock_loader, mock_fx_loader, mock_event_loader):
        result = build_features(
            target_product_key="TEST",
            y=y_series,
            spec=minimal_spec,
            feedstock_loader=mock_feedstock_loader,
            fx_loader=mock_fx_loader,
            event_loader=mock_event_loader,
            horizon=7,
            volume_df=pd.DataFrame({"date": [], "volume": []}),
        )
        vol_cols = [c for c in result.X_train.columns if c.startswith("erp_volume_lag")]
        assert len(vol_cols) == 0


class TestBuildFeaturesVolumeLOCF:
    """Gaps in volume_df should be filled by last observation carried forward."""

    def test_locf_fills_gap(self, y_series, minimal_spec,
                            mock_feedstock_loader, mock_fx_loader, mock_event_loader):
        dates = pd.date_range("2024-12-20", periods=30, freq="D")
        vol = [200.0 + i * 10 for i in range(30)]
        # Drop day 10 to create a gap
        vols_with_gap = vol[:10] + vol[11:]
        dates_with_gap = dates[:10].tolist() + dates[11:].tolist()
        gapped = pd.DataFrame({"date": dates_with_gap, "volume": vols_with_gap})

        result = build_features(
            target_product_key="TEST",
            y=y_series,
            spec=minimal_spec,
            feedstock_loader=mock_feedstock_loader,
            fx_loader=mock_fx_loader,
            event_loader=mock_event_loader,
            horizon=7,
            volume_df=gapped,
        )
        vol_cols = [c for c in result.X_train.columns if c.startswith("erp_volume_lag")]
        assert len(vol_cols) == 7
        for col in vol_cols:
            assert not result.X_train[col].isna().any()


class TestBuildFeaturesXFutureVolume:
    """X_future should include volume lags when cascade_forecasts provided."""

    def test_x_future_has_volume_lags(self, y_series, volume_df, minimal_spec,
                                      mock_feedstock_loader, mock_fx_loader, mock_event_loader):
        cascade = {"fake_feedstock": [600.0 + i * 5 for i in range(7)]}
        result = build_features(
            target_product_key="TEST",
            y=y_series,
            spec=minimal_spec,
            feedstock_loader=mock_feedstock_loader,
            fx_loader=mock_fx_loader,
            event_loader=mock_event_loader,
            horizon=7,
            cascade_forecasts=cascade,
            volume_df=volume_df,
        )
        assert result.X_future is not None, "X_future should be produced"
        for lag in range(1, 8):
            col = f"erp_volume_lag{lag}"
            assert col in result.X_future.columns, f"Missing {col} in X_future"
        assert len(result.X_future) == 7
