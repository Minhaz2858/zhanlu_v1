"""Test conformal VaR and expected magnitude extensions."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.conformal import calibrate, ConformalCalibration


@pytest.fixture
def sample_calibration():
    """Conformal calibration with known half-widths."""
    residuals = {
        3: [5.0, -6.0, 4.0, -5.0, 7.0, -4.0],
        7: [10.0, -12.0, 8.0, -15.0, 11.0, -9.0],
        30: [25.0, -30.0, 22.0, -28.0, 35.0, -20.0],
    }
    return calibrate(residuals, alpha=0.1)


def test_var_95_is_below_point_forecast(sample_calibration):
    """VaR-95% should be below the point forecast (downside risk)."""
    point = 1000.0
    var = sample_calibration.var(point, horizon=7, var_alpha=0.05)
    assert var < point
    assert np.isfinite(var)


def test_var_scales_with_alpha(sample_calibration):
    """Lower var_alpha (more extreme) → more negative VaR."""
    point = 1000.0
    var_95 = sample_calibration.var(point, horizon=7, var_alpha=0.05)
    var_90 = sample_calibration.var(point, horizon=7, var_alpha=0.10)
    var_80 = sample_calibration.var(point, horizon=7, var_alpha=0.20)
    assert var_95 < var_90 < var_80


def test_var_fallback_when_no_calibration():
    """When no calibration for horizon, fallback to sigma fraction."""
    cal = ConformalCalibration(half_widths={}, alpha=0.1)
    var = cal.var(1000.0, horizon=999, var_alpha=0.05)
    # fallback hw = 0.10 * 1000 = 100, scaled by (1-0.05)/(1-0.1) = 1.055...
    expected = 1000.0 - (100 * (0.95 / 0.9))
    assert abs(var - expected) < 0.1


def test_expected_magnitude_returns_half_width(sample_calibration):
    """expected_magnitude returns the calibrated half-width."""
    mag = sample_calibration.expected_magnitude(horizon=7)
    assert mag > 0
    assert np.isfinite(mag)
    # The half-width should be the 90th percentile of abs residuals for h=7
    abs_res = np.abs([10.0, -12.0, 8.0, -15.0, 11.0, -9.0])
    expected_hw = float(np.quantile(abs_res, 0.9))
    assert abs(mag - expected_hw) < 0.01


def test_expected_magnitude_nan_when_no_calibration():
    """When no calibration for horizon, expected_magnitude returns NaN."""
    cal = ConformalCalibration(half_widths={}, alpha=0.1)
    mag = cal.expected_magnitude(horizon=999)
    assert np.isnan(mag)


def test_interval_still_works(sample_calibration):
    """The original interval method is unaffected by the new methods."""
    point = pd.Series([1000.0, 1010.0, 1020.0])
    lo, hi = sample_calibration.interval(point, horizon=3)
    assert (lo < point).all()
    assert (hi > point).all()
    assert (hi - lo > 0).all()
