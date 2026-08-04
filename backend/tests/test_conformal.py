"""Tests for split-conformal prediction intervals (Phase D Task D1)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.forecasting.conformal import calibrate, ConformalCalibration


def test_calibrate_returns_intervals_per_horizon():
    rng = np.random.default_rng(0)
    res = {7: list(rng.normal(0, 3, 200)), 30: list(rng.normal(0, 8, 200))}
    cal = calibrate(res, alpha=0.1)
    assert isinstance(cal, ConformalCalibration)
    point = pd.Series(np.linspace(100, 110, 30))
    lo, hi = cal.interval(point, horizon=7)
    assert len(lo) == 7 and len(hi) == 7
    assert (hi > lo).all()
    assert (lo < point.iloc[:7]).all()
    assert (hi > point.iloc[:7]).all()


def test_wider_interval_for_longer_horizon():
    """30d residuals are larger -> wider interval than 7d."""
    rng = np.random.default_rng(1)
    res = {7: list(rng.normal(0, 2, 200)), 30: list(rng.normal(0, 10, 200))}
    cal = calibrate(res, alpha=0.1)
    point = pd.Series([100.0] * 30)
    lo7, hi7 = cal.interval(point, horizon=7)
    lo30, hi30 = cal.interval(point, horizon=30)
    assert (hi7.iloc[0] - lo7.iloc[0]) < (hi30.iloc[0] - lo30.iloc[0])


def test_empty_residuals_fallback_sigma():
    cal = calibrate({}, alpha=0.1)
    point = pd.Series([100.0] * 7)
    lo, hi = cal.interval(point, horizon=7)
    # Falls back to +/-10% sigma
    assert abs((hi.iloc[0] - lo.iloc[0]) - 20.0) < 0.01


def test_higher_alpha_gives_narrower_interval():
    """alpha=0.3 (70% coverage) is narrower than alpha=0.1 (90%)."""
    rng = np.random.default_rng(2)
    res = {7: list(rng.normal(0, 5, 300))}
    cal90 = calibrate(res, alpha=0.1)
    cal70 = calibrate(res, alpha=0.3)
    point = pd.Series([100.0] * 7)
    _, hi90 = cal90.interval(point, horizon=7)
    _, hi70 = cal70.interval(point, horizon=7)
    assert hi70.iloc[0] < hi90.iloc[0]


def test_scenarios_use_conformal_when_provided():
    """scenarios.generate() should use conformal bands when calibration is given."""
    from app.services.forecasting.scenarios import generate

    point = pd.Series(np.linspace(100, 110, 30))
    res = {7: list(np.random.default_rng(2).normal(0, 3, 100))}
    cal = calibrate(res, alpha=0.1)
    sc = generate(point, residuals=None, horizons=[7], calibration=cal)
    base = sc.horizons[7]["base"]
    bull = sc.horizons[7]["bull"]
    bear = sc.horizons[7]["bear"]
    assert (bull > base).all()
    assert (bear < base).all()
    assert sc.bounds_source == "conformal"
