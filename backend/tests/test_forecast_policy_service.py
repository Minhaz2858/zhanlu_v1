"""Tests for Phase 4: Forecast Policy Service.

Verifies:
- Bias correction from ForecastAccuracyLog
- Volatility regime detection
- Policy application returns adjusted values within safe bounds
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest

from app.services.forecasting.forecast_policy_service import (
    ForecastPolicyMetrics,
    ForecastPolicyService,
    VolatilityRegime,
)


class TestForecastPolicyMetrics:
    """ForecastPolicyMetrics dataclass behavior."""

    def test_baseline_metrics_are_neutral(self):
        m = ForecastPolicyMetrics.create_baseline()
        assert m.bias_pct == 0.0
        assert m.volatility_regime == VolatilityRegime.NORMAL
        assert m.vol_multiplier == 1.0
        assert m.sample_count == 0

    def test_regime_detection_empty_returns_normal(self):
        regime, mult = ForecastPolicyService.detect_volatility_regime([])
        assert regime == VolatilityRegime.NORMAL
        assert mult == 1.0

    def test_regime_detection_high_vol(self):
        # ~5.5% daily std → HIGH regime
        returns = [0.10, -0.08, 0.09, -0.07, 0.06, -0.09, 0.08, -0.10]
        regime, mult = ForecastPolicyService.detect_volatility_regime(returns)
        assert regime == VolatilityRegime.HIGH
        assert mult == 1.25

    def test_regime_detection_moderate_vol(self):
        # ~2.5% daily std → MODERATE regime
        returns = [0.03, -0.025, 0.02, -0.03, 0.025, -0.02, 0.03, -0.025]
        regime, mult = ForecastPolicyService.detect_volatility_regime(returns)
        assert regime == VolatilityRegime.MODERATE
        assert mult == 1.1

    def test_regime_detection_low_vol(self):
        # ~0.5% daily std → NORMAL regime
        returns = [0.005, -0.004, 0.006, -0.005, 0.004, -0.006, 0.005, -0.004]
        regime, mult = ForecastPolicyService.detect_volatility_regime(returns)
        assert regime == VolatilityRegime.NORMAL
        assert mult == 1.0


class TestBiasCorrection:
    """Bias correction from historical accuracy data."""

    def test_no_data_returns_zero_bias(self):
        bias = ForecastPolicyService.compute_bias_pct([])
        assert bias == 0.0

    def test_positive_bias_corrects_downward(self):
        # Mean signed error +3% → bias correction negative
        errors = [0.03, 0.02, 0.04, 0.03]
        bias = ForecastPolicyService.compute_bias_pct(errors)
        assert bias < 0

    def test_negative_bias_corrects_upward(self):
        # Mean signed error -2% → bias correction positive
        errors = [-0.02, -0.01, -0.03, -0.02]
        bias = ForecastPolicyService.compute_bias_pct(errors)
        assert bias > 0

    def test_bias_is_capped_at_2_5_percent(self):
        # Very large error → bias capped at ±2.5%
        errors = [0.10] * 10
        bias = ForecastPolicyService.compute_bias_pct(errors)
        assert abs(bias) <= 2.5

    def test_bias_is_capped_at_negative_2_5(self):
        errors = [-0.10] * 10
        bias = ForecastPolicyService.compute_bias_pct(errors)
        assert abs(bias) <= 2.5


class TestPolicyApply:
    """PolicyService.apply() tests."""

    def test_apply_with_baseline_metrics_returns_unchanged(self):
        raw = {3: [100.0, 101.0], 7: [102.0, 104.0], 30: [105.0, 108.0]}
        metrics = ForecastPolicyMetrics.create_baseline()
        adjusted, detail = ForecastPolicyService.apply(raw, metrics)
        # Values should be approximately unchanged
        for h in [3, 7, 30]:
            assert adjusted[h] == raw[h]

    def test_apply_with_bullish_bias_increases_forecast(self):
        raw = {3: [100.0, 100.0], 7: [100.0, 100.0], 30: [100.0, 100.0]}
        metrics = ForecastPolicyMetrics(
            bias_pct=2.0,
            volatility_regime=VolatilityRegime.NORMAL,
            vol_multiplier=1.0,
            diagnosis_bias=0.0,
            sample_count=10,
            mean_signed_error=-0.025,
        )
        adjusted, detail = ForecastPolicyService.apply(raw, metrics)
        # Bias correction for -2.5% error → +0.875% * 0.35 = +0.31 or so
        # Actually: bias_pct = max(-2.5, min(2.5, -(-0.025) * 0.35)) = +0.875
        # So 7d: 100 * (1 + 0.875/100) ≈ 100.875
        assert adjusted[7][0] > 100.0

    def test_policy_detail_included(self):
        raw = {7: [100.0]}
        metrics = ForecastPolicyMetrics.create_baseline()
        _, detail = ForecastPolicyService.apply(raw, metrics)
        assert "bias_pct" in detail
        assert "volatility_regime" in detail
        assert "horizon_adjustment" in detail
