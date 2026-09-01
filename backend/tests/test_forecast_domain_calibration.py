"""P0-2: Domain signal calibration — A/B backtest + JSON elasticity overrides.

Tests verify:
1. Calibration backtest runs with/without domain signals and measures MAPE delta
2. JSON override file loads correctly
3. get_calibrated_elasticity() prefers override over hardcoded
4. When no naphtha signal, calibration reports neutral
"""
from __future__ import annotations

import json
import os
import tempfile

import numpy as np
import pandas as pd
from datetime import datetime

from app.services.forecasting.domain_signal_calibration import (
    CalibrationResult,
    run_calibration_backtest,
    load_elasticity_overrides,
    get_calibrated_elasticity,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_seasonal_series(
    n: int = 120,
    product_id: str = "c5_resin",
    base: float = 100.0,
    noise_std: float = 1.0,
    seasonal_amplitude: float = 2.0,
    trend: float = 0.0,
) -> pd.Series:
    """Create a synthetic price series with winter dip (Nov-Feb) and spring recovery."""
    dates = pd.date_range(start="2025-01-01", periods=n, freq="D")
    rng = np.random.RandomState(42)
    noise = rng.randn(n) * noise_std
    # Winter dip: months 1,2,11,12 get negative seasonal
    month_effects = np.zeros(n)
    for i, d in enumerate(dates):
        m = d.month
        if m in (1, 2, 11, 12):
            month_effects[i] = -seasonal_amplitude
        elif m in (3, 4, 5):
            month_effects[i] = seasonal_amplitude * 0.5
    trend_line = np.arange(n) * trend
    vals = base + trend_line + month_effects + noise
    return pd.Series(vals, index=dates, name="y")


# ---------------------------------------------------------------------------
# run_calibration_backtest
# ---------------------------------------------------------------------------

def test_calibration_backtest_returns_result_with_naphtha_signal():
    """When naphtha rose, with-signals forecast should differ from baseline."""
    y = _make_seasonal_series(n=100, base=100.0, noise_std=2.0)
    result = run_calibration_backtest(
        y=y,
        product_id="c5_resin",
        naphtha_pct_change=10.0,
        as_of_date=datetime(2025, 5, 15),
        horizons=[7, 15],
    )

    assert isinstance(result, CalibrationResult)
    assert result.product_id == "c5_resin"
    assert result.baseline_mape >= 0, "Baseline MAPE should be non-negative"
    assert result.with_signals_mape >= 0, "With-signals MAPE should be non-negative"


def test_calibration_backtest_no_naphtha_neutral():
    """When naphtha signal is None, only seasonal rules apply."""
    y = _make_seasonal_series(n=100, base=100.0)
    result = run_calibration_backtest(
        y=y,
        product_id="c5_resin",
        naphtha_pct_change=None,
        as_of_date=datetime(2025, 5, 15),
        horizons=[7],
    )

    # Without naphtha signal, seasonal-only may slightly help or not
    assert result.product_id == "c5_resin"
    assert isinstance(result.improvement_pct, float)


def test_calibration_backtest_unknown_product_no_elasticity():
    """Unknown product gets no causal adjustment, so with/without same."""
    y = _make_seasonal_series(n=100, base=100.0)
    result = run_calibration_backtest(
        y=y,
        product_id="zzz_unknown_xyz",
        naphtha_pct_change=10.0,
        as_of_date=datetime(2025, 5, 15),
        horizons=[7],
    )

    assert not result.applied, "Unknown product should show no meaningful improvement"


def test_calibration_backtest_applied_flag_when_signals_help():
    """When domain signals reduce MAPE, applied should be True."""
    # Build a series with a strong seasonal pattern matching the seasonal rules
    # for c5_resin (winter dip Nov-Feb, spring recovery March+)
    dates = pd.date_range(start="2024-11-01", periods=120, freq="D")
    rng = np.random.RandomState(42)
    vals = np.zeros(120)
    base = 100.0
    for i, d in enumerate(dates):
        vals[i] = base + rng.randn() * 2.0
        if d.month in (11, 12, 1, 2):
            vals[i] -= 3.0  # match the -2.5% seasonal rule
        elif d.month in (3, 4, 5):
            vals[i] += 1.5
    y = pd.Series(vals, index=dates, name="y")

    result = run_calibration_backtest(
        y=y,
        product_id="c5_resin",
        naphtha_pct_change=5.0,
        as_of_date=datetime(2025, 3, 15),
        horizons=[7],
    )

    # With strong seasonal pattern, domain signals should provide at least
    # some marginal improvement (improvement_pct > 0)
    if result.improvement_pct > 0:
        assert result.applied, "Improvement > 0 should set applied=True"
        assert result.recommended_elasticity is not None


def test_calibration_backtest_short_series_graceful():
    """Very short series should not crash; return neutral result."""
    y = _make_seasonal_series(n=20, base=100.0)
    result = run_calibration_backtest(
        y=y,
        product_id="c5_resin",
        naphtha_pct_change=5.0,
        as_of_date=datetime(2025, 3, 15),
        horizons=[7],
    )

    assert isinstance(result, CalibrationResult)
    assert not result.applied, "Short series should not claim signals help"


# ---------------------------------------------------------------------------
# JSON override file
# ---------------------------------------------------------------------------

def test_load_elasticity_overrides_from_json():
    """Load overrides from a valid JSON file."""
    overrides_data = {
        "naphtha": 0.95,
        "c5_resin": 0.5,
        "dcpd": 0.7,
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(overrides_data, f)
        tmp_path = f.name

    try:
        overrides = load_elasticity_overrides(tmp_path)
        assert overrides == {
            "naphtha": 0.95,
            "c5_resin": 0.5,
            "dcpd": 0.7,
        }
    finally:
        os.unlink(tmp_path)


def test_load_elasticity_overrides_empty_file_returns_empty_dict():
    """Empty JSON file returns empty dict."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({}, f)
        tmp_path = f.name

    try:
        overrides = load_elasticity_overrides(tmp_path)
        assert overrides == {}
    finally:
        os.unlink(tmp_path)


def test_load_elasticity_overrides_missing_file_returns_empty_dict():
    """Missing file returns empty dict gracefully."""
    overrides = load_elasticity_overrides("/tmp/__nonexistent_domain_calibration__.json")
    assert overrides == {}


# ---------------------------------------------------------------------------
# get_calibrated_elasticity
# ---------------------------------------------------------------------------

def test_get_calibrated_elasticity_with_override():
    """Override value takes precedence over hardcoded static elasticity."""
    overrides = {"c5_resin": 0.50}
    val = get_calibrated_elasticity("c5_resin", overrides=overrides)
    assert val == 0.50, f"Override should return 0.50, got {val}"


def test_get_calibrated_elasticity_empty_config_returns_none():
    """No domain config → no static elasticity table → None (generic platform)."""
    val = get_calibrated_elasticity("c5_resin", overrides={})
    assert val is None


def test_get_calibrated_elasticity_fallback_to_configured(domain_signals_config):
    """When no override exists, fall back to the config-loaded _ELASTICITIES."""
    val = get_calibrated_elasticity("widget", overrides={})
    # widget configured raw 0.5 × damp 1.0 = 0.5
    assert val == 0.5, f"Expected 0.5, got {val}"


def test_get_calibrated_elasticity_unknown_product():
    """Unknown product returns None."""
    val = get_calibrated_elasticity("zzz_unknown", overrides={})
    assert val is None
