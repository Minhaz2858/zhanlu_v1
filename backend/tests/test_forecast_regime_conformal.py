"""Test regime-aware conformal calibration (P2-1C).

Validates that RegimeAwareConformalCalibration produces wider intervals
for volatile regimes and falls back to global calibration when regime
has insufficient data.
"""
import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.conformal import (
    calibrate_regime_aware,
    RegimeAwareConformalCalibration,
)


class TestRegimeAwareConformalCalibration:
    """Tests for RegimeAwareConformalCalibration."""

    def _make_residuals(self, n=50, mean=0, std=5, seed=42):
        """Generate synthetic residuals."""
        np.random.seed(seed)
        return list(np.random.normal(mean, std, n))

    def test_volatile_regime_wider_than_stable(self):
        """Volatile regime should have wider half-widths than stable."""
        # Stable: small residuals (std=2)
        # Volatile: large residuals (std=10)
        residuals = {
            "stable": {1: self._make_residuals(50, std=2, seed=1)},
            "volatile": {1: self._make_residuals(50, std=10, seed=2)},
        }
        global_res = {1: self._make_residuals(100, std=5, seed=3)}

        cal = calibrate_regime_aware(residuals, global_res, alpha=0.1)

        stable_hw = cal._get_half_width(1, "stable")
        volatile_hw = cal._get_half_width(1, "volatile")

        assert stable_hw is not None
        assert volatile_hw is not None
        assert volatile_hw > stable_hw

    def test_unknown_regime_uses_fallback_multiplier(self):
        """Unknown regime should use global calibration with multiplier."""
        residuals = {
            "stable": {1: self._make_residuals(50, std=2, seed=1)},
        }
        global_res = {1: self._make_residuals(100, std=5, seed=3)}

        cal = calibrate_regime_aware(residuals, global_res, alpha=0.1)
        cal.regime_fallback_multiplier = 1.5

        global_hw = cal._get_half_width(1, None)
        unknown_hw = cal._get_half_width(1, "unknown_regime")

        assert unknown_hw is not None
        assert global_hw is not None
        assert unknown_hw == pytest.approx(global_hw * 1.5, rel=0.01)

    def test_insufficient_samples_skips_regime(self):
        """Regime with too few samples should not be calibrated."""
        residuals = {
            "stable": {1: self._make_residuals(50, std=2, seed=1)},
            "sparse": {1: self._make_residuals(3, std=10, seed=2)},  # < min_samples=5
        }
        global_res = {1: self._make_residuals(100, std=5, seed=3)}

        cal = calibrate_regime_aware(
            residuals, global_res, alpha=0.1, min_samples_per_regime=5,
        )

        assert "stable" in cal.regime_half_widths
        assert "sparse" not in cal.regime_half_widths

    def test_interval_produces_bounds(self):
        """interval() should produce lower < upper bounds."""
        residuals = {
            "volatile": {1: self._make_residuals(50, std=10, seed=1)},
        }
        global_res = {1: self._make_residuals(100, std=5, seed=2)}

        cal = calibrate_regime_aware(residuals, global_res, alpha=0.1)
        point_fc = pd.Series([100.0, 101.0, 102.0])

        lo, hi = cal.interval(point_fc, horizon=1, regime="volatile")
        assert len(lo) == 1
        assert len(hi) == 1
        assert lo.iloc[0] < hi.iloc[0]
        assert lo.iloc[0] < point_fc.iloc[0]
        assert hi.iloc[0] > point_fc.iloc[0]

    def test_var_downside_risk(self):
        """var() should be less than point forecast (downside)."""
        residuals = {
            "volatile": {1: self._make_residuals(50, std=10, seed=1)},
        }
        global_res = {1: self._make_residuals(100, std=5, seed=2)}

        cal = calibrate_regime_aware(residuals, global_res, alpha=0.1)
        point = 100.0

        var_volatile = cal.var(point, horizon=1, regime="volatile", var_alpha=0.05)
        var_stable = cal.var(point, horizon=1, regime="stable", var_alpha=0.05)

        # VaR should be below the point forecast
        assert var_volatile < point
        # Volatile VaR should be lower (more conservative) than stable
        # (but stable has no calibration, so it uses fallback)

    def test_expected_magnitude(self):
        """expected_magnitude() should return the half-width."""
        residuals = {
            "stable": {1: self._make_residuals(50, std=2, seed=1)},
        }
        global_res = {1: self._make_residuals(100, std=5, seed=2)}

        cal = calibrate_regime_aware(residuals, global_res, alpha=0.1)
        mag = cal.expected_magnitude(horizon=1, regime="stable")
        assert mag > 0
        assert np.isfinite(mag)

    def test_empty_regimes_falls_back_to_global(self):
        """When no regimes have data, should still produce global calibration."""
        residuals = {}
        global_res = {1: self._make_residuals(100, std=5, seed=1)}

        cal = calibrate_regime_aware(residuals, global_res, alpha=0.1)
        hw = cal._get_half_width(1, None)
        assert hw is not None
        assert hw > 0
