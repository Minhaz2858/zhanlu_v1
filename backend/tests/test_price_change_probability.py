"""Tests for price-change probability (Phase D Task D2)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.forecasting.conformal import ConformalCalibration
from app.services.forecasting.price_change_probability import (
    compute, PriceChangeProbability,
)


def test_p_rise_above_half_when_forecast_up():
    cal = ConformalCalibration(half_widths={7: 3.0})
    point = pd.Series([105.0] * 7)  # forecast rises from 100
    res = compute(point, last_actual=100.0, calibration=cal, horizon=7)
    assert res.p_rise > 0.5
    assert 0.0 <= res.p_rise <= 1.0


def test_p_rise_below_half_when_forecast_down():
    cal = ConformalCalibration(half_widths={7: 3.0})
    point = pd.Series([96.0] * 7)
    res = compute(point, last_actual=100.0, calibration=cal, horizon=7)
    assert res.p_rise < 0.5


def test_threshold_probabilities():
    """P(rise > 5%) should be < P(rise > 0%)."""
    cal = ConformalCalibration(half_widths={30: 8.0})
    point = pd.Series([104.0] * 30)
    res = compute(
        point, last_actual=100.0, calibration=cal, horizon=30,
        thresholds=[0.0, 0.03, 0.05],
    )
    assert res.p_rise_gt[0.0] > res.p_rise_gt[0.05]


def test_expected_change_pct():
    cal = ConformalCalibration(half_widths={7: 3.0})
    point = pd.Series([106.0] * 7)
    res = compute(point, last_actual=100.0, calibration=cal, horizon=7)
    assert abs(res.expected_change_pct - 0.06) < 1e-9


def test_fallback_when_no_calibration():
    """No half-width for the horizon -> sigma fallback still produces a probability."""
    cal = ConformalCalibration(half_widths={})
    point = pd.Series([110.0] * 7)
    res = compute(point, last_actual=100.0, calibration=cal, horizon=7)
    assert 0.0 <= res.p_rise <= 1.0
    assert res.p_rise > 0.5  # 10% rise with fallback sigma -> likely up
