"""P2.14: Unified bias correction + aligned volatility thresholds.

Two changes:
1. Bias correction: forecast_policy_service remains the ONLY data-driven bias
   path (damping 0.35, cap ±2.5%). ops/bias_correction HITL delta is routed
   THROUGH the policy service budget (combined absolute cap ±2.5%).
2. Volatility thresholds: guard_advanced.py imports canonical thresholds
   from forecast_policy_service (single source of truth: 5%/1.5%).
"""
import pytest

from app.services.forecasting.forecast_policy_service import (
    FORECAST_BIAS_CAP_PCT,
    HIGH_VOL_THRESHOLD,
    MODERATE_VOL_THRESHOLD,
    apply_bias_correction,
)
from app.services.forecasting.guard_advanced import (
    HIGH_VOL_THRESHOLD as GUARD_HIGH_VOL,
    MODERATE_VOL_THRESHOLD as GUARD_MODERATE_VOL,
)


class TestUnifiedBiasCorrection:
    """Bias correction must have a single cap, not two compounding ones."""

    def test_single_bias_cap_is_2_5_pct(self):
        """The combined bias cap must be ±2.5%."""
        assert FORECAST_BIAS_CAP_PCT == 2.5

    def test_hitl_delta_routed_through_policy_budget(self):
        """HITL author delta must compete for the same ±2.5% budget."""
        # When the policy service has already applied +2.0% bias,
        # an HITL delta of +1.5% should be capped to +0.5%
        # (total = 2.5%, not 3.5%).
        result = apply_bias_correction(
            forecast_value=100.0,
            accuracy_log_bias_pct=2.0,   # already applied 2.0%
            hitl_delta_pct=1.5,           # wants +1.5% more
        )
        # Total adjustment should be exactly 2.5%, not 3.5%
        assert result == pytest.approx(102.5, abs=0.01)

    def test_hitl_negative_delta_combined(self):
        """Negative HITL delta combined with positive accuracy bias."""
        result = apply_bias_correction(
            forecast_value=100.0,
            accuracy_log_bias_pct=1.5,
            hitl_delta_pct=-2.0,
        )
        # Net adjustment = 1.5 + (-2.0) = -0.5%, within cap
        assert result == pytest.approx(99.5, abs=0.01)

    def test_hitl_and_accuracy_both_negative_capped(self):
        """Large negative combined adjustments are capped at -2.5%."""
        result = apply_bias_correction(
            forecast_value=100.0,
            accuracy_log_bias_pct=-1.5,
            hitl_delta_pct=-2.0,
        )
        # Net = -3.5%, capped to -2.5%
        assert result == pytest.approx(97.5, abs=0.01)


class TestAlignedVolThresholds:
    """Volatility thresholds must be consistent across modules."""

    def test_guard_imports_policy_thresholds(self):
        """guard_advanced must import thresholds from policy_service."""
        assert GUARD_HIGH_VOL == HIGH_VOL_THRESHOLD
        assert GUARD_MODERATE_VOL == MODERATE_VOL_THRESHOLD

    def test_thresholds_are_sensible(self):
        """Canonical thresholds: 5% high, 1.5% moderate."""
        assert HIGH_VOL_THRESHOLD == 5.0
        assert MODERATE_VOL_THRESHOLD == 1.5
        assert HIGH_VOL_THRESHOLD > MODERATE_VOL_THRESHOLD
