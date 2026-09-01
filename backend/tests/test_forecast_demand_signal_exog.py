"""P0-1: Wire DemandSignal + SupplierDispersion columns into build_features().

Tests verify that demand-side metrics and supplier ladder signals appear as
exogenous feature columns when the new optional params are provided, and that
backward compatibility is preserved when they are not.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from datetime import datetime, timedelta

from app.services.forecasting.features.feature_builder import build_features, FeatureMatrix
from app.services.forecasting.features.feature_registry import FeatureSpec
from app.services.forecasting.features.demand_signal import DemandSignal


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _dummy_spec() -> FeatureSpec:
    return FeatureSpec(
        product_key="test_product",
        feedstock_keys=["feed_a"],
        feedstock_lags=[1, 3, 7],
        spread_pairs=[],
        use_fx=False,
        calendar_features=False,
    )


def _dummy_y(n: int = 60) -> pd.Series:
    dates = pd.date_range(start="2025-01-01", periods=n, freq="D")
    return pd.Series(np.random.randn(n).cumsum() + 100.0, index=dates, name="y")


def _dummy_feedstock_loader():
    class Loader:
        def read_actuals(self, fk, start, end):
            dates = pd.date_range(start=start, end=end, freq="D")
            vals = np.random.randn(len(dates)).cumsum() + 50.0
            df = pd.DataFrame({"ds": dates, fk: vals})
            return df
    return Loader()


def _dummy_fx_loader():
    class Loader:
        def read_usd_cny(self, start, end):
            dates = pd.date_range(start=start, end=end, freq="D")
            df = pd.DataFrame({"ds": dates, "usd_cny": 7.2})
            return df
    return Loader()


def _dummy_event_loader():
    class Loader:
        def read_flags(self, pk, start, end):
            dates = pd.date_range(start=start, end=end, freq="D")
            df = pd.DataFrame({"ds": dates, "event_flag": 0.0})
            return df
    return Loader()


# ---------------------------------------------------------------------------
# demand signal feature wiring
# ---------------------------------------------------------------------------

def test_demand_signal_columns_wired_when_provided():
    """DemandSignal fields appear as feature columns when demand_signal kwarg is passed."""
    y = _dummy_y(60)
    demand = DemandSignal(
        product_id="test_product",
        vol_momentum_4wk=12.5,
        yoy_change_pct=8.0,
        vol_price_divergence=3.2,
        demand_trend="rising",
        has_sufficient_data=True,
    )

    result = build_features(
        target_product_key="test_product",
        y=y,
        spec=_dummy_spec(),
        feedstock_loader=_dummy_feedstock_loader(),
        fx_loader=_dummy_fx_loader(),
        event_loader=_dummy_event_loader(),
        horizon=7,
        demand_signal=demand,
    )

    assert result.X_train is not None, "X_train should not be None"
    feature_names = result.X_train.columns.tolist()

    # Demand signal columns must be present
    assert "demand_trend_encoded" in feature_names, f"Missing demand_trend_encoded in {feature_names}"
    assert "vol_momentum_4wk" in feature_names, f"Missing vol_momentum_4wk in {feature_names}"
    assert "yoy_change_pct" in feature_names, f"Missing yoy_change_pct in {feature_names}"
    assert "vol_price_divergence" in feature_names, f"Missing vol_price_divergence in {feature_names}"

    # demand_trend_encoded should be 1.0 for "rising"
    encoded_vals = result.X_train["demand_trend_encoded"].unique()
    assert encoded_vals.tolist() == [1.0], f"Expected [1.0] for 'rising', got {encoded_vals.tolist()}"

    # Momentum numeric values should be set
    assert result.X_train["vol_momentum_4wk"].iloc[0] == 12.5
    assert result.X_train["yoy_change_pct"].iloc[0] == 8.0
    assert result.X_train["vol_price_divergence"].iloc[0] == 3.2


def test_demand_signal_trend_encoded_falling():
    """demand_trend_encoded is -1 for falling, 0 for stable."""
    y = _dummy_y(40)
    y.index.freq = "D"

    for trend, expected in [("falling", -1.0), ("stable", 0.0), ("rising", 1.0)]:
        demand = DemandSignal(
            product_id="tp",
            demand_trend=trend,
            has_sufficient_data=True,
        )
        result = build_features(
            target_product_key="tp",
            y=y,
            spec=_dummy_spec(),
            feedstock_loader=_dummy_feedstock_loader(),
            fx_loader=_dummy_fx_loader(),
            event_loader=_dummy_event_loader(),
            horizon=7,
            demand_signal=demand,
        )
        assert result.X_train["demand_trend_encoded"].iloc[0] == expected, \
            f"demand_trend='{trend}' should encode to {expected}"


def test_demand_signal_none_values_filled_with_zero():
    """When DemandSignal has None for numeric fields, columns still exist with 0.0."""
    y = _dummy_y(40)
    demand = DemandSignal(
        product_id="tp",
        # All numeric default None
        demand_trend="stable",
        has_sufficient_data=False,
    )

    result = build_features(
        target_product_key="tp",
        y=y,
        spec=_dummy_spec(),
        feedstock_loader=_dummy_feedstock_loader(),
        fx_loader=_dummy_fx_loader(),
        event_loader=_dummy_event_loader(),
        horizon=7,
        demand_signal=demand,
    )

    assert result.X_train is not None
    assert result.X_train["vol_momentum_4wk"].iloc[0] == 0.0
    assert result.X_train["yoy_change_pct"].iloc[0] == 0.0
    assert result.X_train["vol_price_divergence"].iloc[0] == 0.0
    assert result.X_train["demand_trend_encoded"].iloc[0] == 0.0


def test_no_demand_signal_keeps_backward_compat():
    """build_features without demand_signal should not add any demand columns."""
    y = _dummy_y(60)
    result = build_features(
        target_product_key="tp",
        y=y,
        spec=_dummy_spec(),
        feedstock_loader=_dummy_feedstock_loader(),
        fx_loader=_dummy_fx_loader(),
        event_loader=_dummy_event_loader(),
        horizon=7,
    )

    feature_names = result.X_train.columns.tolist()
    demand_cols = {"demand_trend_encoded", "vol_momentum_4wk", "yoy_change_pct", "vol_price_divergence"}
    for col in demand_cols:
        assert col not in feature_names, f"Column '{col}' should NOT appear without demand_signal kwarg"


# ---------------------------------------------------------------------------
# supplier dispersion feature wiring
# ---------------------------------------------------------------------------

def test_supplier_dispersion_columns_wired_when_provided():
    """Supplier dispersion columns appear when supplier_dispersion_df is passed."""
    y = _dummy_y(60)
    dates = pd.date_range(start="2025-01-01", periods=45, freq="D")
    disp_df = pd.DataFrame({
        "date": dates,
        "spread": np.random.uniform(50, 150, len(dates)),
        "supplier_count": np.random.randint(3, 12, len(dates)),
    })

    result = build_features(
        target_product_key="tp",
        y=y,
        spec=_dummy_spec(),
        feedstock_loader=_dummy_feedstock_loader(),
        fx_loader=_dummy_fx_loader(),
        event_loader=_dummy_event_loader(),
        horizon=7,
        supplier_dispersion_df=disp_df,
    )

    assert result.X_train is not None
    feature_names = result.X_train.columns.tolist()
    assert "supplier_spread" in feature_names, f"Missing supplier_spread in {feature_names}"
    assert "supplier_count" in feature_names, f"Missing supplier_count in {feature_names}"

    # Values should be consistent (latest data)
    spread_vals = result.X_train["supplier_spread"]
    assert spread_vals.nunique() == 1, "supplier_spread should be constant per training cycle"
    supplier_cnt_vals = result.X_train["supplier_count"]
    assert supplier_cnt_vals.nunique() == 1


def test_no_supplier_df_keeps_backward_compat():
    """build_features without supplier_dispersion_df should not add supplier columns."""
    y = _dummy_y(60)
    result = build_features(
        target_product_key="tp",
        y=y,
        spec=_dummy_spec(),
        feedstock_loader=_dummy_feedstock_loader(),
        fx_loader=_dummy_fx_loader(),
        event_loader=_dummy_event_loader(),
        horizon=7,
    )

    feature_names = result.X_train.columns.tolist()
    for col in ("supplier_spread", "supplier_count"):
        assert col not in feature_names, f"Column '{col}' should NOT appear without supplier_dispersion_df"


# ---------------------------------------------------------------------------
# combined wiring
# ---------------------------------------------------------------------------

def test_demand_and_supplier_columns_both_present():
    """When both demand_signal and supplier_dispersion_df are provided, all columns appear."""
    y = _dummy_y(60)
    demand = DemandSignal(
        product_id="tp",
        vol_momentum_4wk=-5.0,
        yoy_change_pct=-3.0,
        vol_price_divergence=1.0,
        demand_trend="falling",
        has_sufficient_data=True,
    )
    dates = pd.date_range(start="2025-01-01", periods=45, freq="D")
    disp_df = pd.DataFrame({
        "date": dates,
        "spread": [80.0] * len(dates),
        "supplier_count": [5.0] * len(dates),
    })

    result = build_features(
        target_product_key="tp",
        y=y,
        spec=_dummy_spec(),
        feedstock_loader=_dummy_feedstock_loader(),
        fx_loader=_dummy_fx_loader(),
        event_loader=_dummy_event_loader(),
        horizon=7,
        demand_signal=demand,
        supplier_dispersion_df=disp_df,
    )

    assert result.X_train is not None
    feature_names = result.X_train.columns.tolist()

    demand_cols = ["demand_trend_encoded", "vol_momentum_4wk", "yoy_change_pct", "vol_price_divergence"]
    supplier_cols = ["supplier_spread", "supplier_count"]
    for col in demand_cols + supplier_cols:
        assert col in feature_names, f"Missing column '{col}' in combined mode"

    # Check values
    assert result.X_train["demand_trend_encoded"].iloc[0] == -1.0
    assert result.X_train["vol_momentum_4wk"].iloc[0] == -5.0
    assert result.X_train["supplier_spread"].iloc[0] == 80.0
    assert result.X_train["supplier_count"].iloc[0] == 5.0


# ---------------------------------------------------------------------------
# feature_names in result
# ---------------------------------------------------------------------------

def test_feature_names_includes_demand_and_supplier():
    """FeatureMatrix.feature_names lists all demand + supplier columns."""
    y = _dummy_y(60)
    demand = DemandSignal(
        product_id="tp",
        vol_momentum_4wk=3.0,
        demand_trend="stable",
        has_sufficient_data=True,
    )
    dates = pd.date_range(start="2025-01-01", periods=45, freq="D")
    disp_df = pd.DataFrame({
        "date": dates,
        "spread": [100.0] * len(dates),
        "supplier_count": [3.0] * len(dates),
    })

    result = build_features(
        target_product_key="tp",
        y=y,
        spec=_dummy_spec(),
        feedstock_loader=_dummy_feedstock_loader(),
        fx_loader=_dummy_fx_loader(),
        event_loader=_dummy_event_loader(),
        horizon=7,
        demand_signal=demand,
        supplier_dispersion_df=disp_df,
    )

    for col in ["demand_trend_encoded", "vol_momentum_4wk", "supplier_spread", "supplier_count"]:
        assert col in result.feature_names, f"Missing '{col}' in feature_names"


# ---------------------------------------------------------------------------
# X_future: demand/supplier columns carried forward
# ---------------------------------------------------------------------------

def test_demand_signal_cols_in_x_future_when_cascade_provided():
    """When cascade_forecasts is provided, X_future carries demand signal columns."""
    y = _dummy_y(60)
    demand = DemandSignal(
        product_id="tp",
        vol_momentum_4wk=7.0,
        demand_trend="rising",
        has_sufficient_data=True,
    )
    cascade = {"feed_a": [55.0 + i for i in range(7)]}

    result = build_features(
        target_product_key="tp",
        y=y,
        spec=_dummy_spec(),
        feedstock_loader=_dummy_feedstock_loader(),
        fx_loader=_dummy_fx_loader(),
        event_loader=_dummy_event_loader(),
        horizon=7,
        cascade_forecasts=cascade,
        demand_signal=demand,
    )

    assert result.X_future is not None, "X_future should be generated when cascade is provided"
    assert "demand_trend_encoded" in result.X_future.columns
    assert "vol_momentum_4wk" in result.X_future.columns
    # All future rows should carry the same value (last known state)
    assert (result.X_future["demand_trend_encoded"] == 1.0).all()
    assert (result.X_future["vol_momentum_4wk"] == 7.0).all()
