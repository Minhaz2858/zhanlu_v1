"""P2.13: Isotonic p_rise calibration layer.

Calibrates predicted_p_rise from ForecastDecisionLog against realized
direction (did the price actually rise?). Uses sklearn.isotonic for
monotone calibration.  Falls back to P0.3 empirical p_rise when
insufficient samples exist.

The calibration is flag-gated (FORECAST_P_RISE_CALIBRATION_ENABLED, default
false) and runs as a nightly step after eval + decision-scoring.
"""
import numpy as np
import pytest

from app.services.forecasting.ops.p_rise_calibration import (
    CalibratedPRiseResult,
    fit_product_calibration,
    apply_calibration,
)


class TestPRiseCalibration:
    """Isotonic calibration of p_rise."""

    def test_module_importable(self):
        """The calibration module must be importable."""
        assert callable(fit_product_calibration)
        assert callable(apply_calibration)

    def test_fit_with_enough_samples(self):
        """With ≥30 scored decisions, calibration should succeed."""
        rng = np.random.RandomState(42)
        n = 50
        # Generate correlated predicted_p_rise and actual outcomes
        predicted = rng.uniform(0.1, 0.9, n)
        # Simulate: higher predicted → higher actual probability
        actual = (rng.rand(n) < predicted).astype(float)

        rows = [
            {"predicted_p_rise": float(p), "actual_rise": bool(a)}
            for p, a in zip(predicted, actual)
        ]
        result = fit_product_calibration(rows, min_samples=30)
        assert result is not None
        assert result["n"] >= 30
        assert "curve" in result
        assert "reliability" in result

    def test_fit_too_few_samples_returns_none(self):
        """With < min_samples, calibration should return None (fall back to empirical)."""
        rows = [
            {"predicted_p_rise": 0.6, "actual_rise": True},
            {"predicted_p_rise": 0.4, "actual_rise": False},
        ]
        result = fit_product_calibration(rows, min_samples=30)
        assert result is None

    def test_apply_calibration_monotone(self):
        """Calibrated p_rise must be monotonically increasing."""
        rng = np.random.RandomState(42)
        n = 100
        predicted = rng.uniform(0.1, 0.9, n)
        actual = (rng.rand(n) < predicted).astype(float)
        rows = [
            {"predicted_p_rise": float(p), "actual_rise": bool(a)}
            for p, a in zip(predicted, actual)
        ]
        result = fit_product_calibration(rows, min_samples=30)
        assert result is not None

        # Apply to a range of input values
        test_values = np.linspace(0.1, 0.9, 20)
        calibrated = [apply_calibration(p, result) for p in test_values]
        # Should be monotonically non-decreasing
        for i in range(1, len(calibrated)):
            assert calibrated[i] >= calibrated[i - 1] - 0.01, (
                f"Calibration not monotone: {calibrated[i-1]:.4f} > {calibrated[i]:.4f}"
            )

    def test_apply_calibration_out_of_range_clamps(self):
        """Values outside the training range should be clamped to [0, 1]."""
        rng = np.random.RandomState(42)
        n = 50
        predicted = rng.uniform(0.2, 0.8, n)
        actual = (rng.rand(n) < predicted).astype(float)
        rows = [
            {"predicted_p_rise": float(p), "actual_rise": bool(a)}
            for p, a in zip(predicted, actual)
        ]
        result = fit_product_calibration(rows, min_samples=30)
        assert result is not None

        assert 0.0 <= apply_calibration(0.0, result) <= 1.0
        assert 0.0 <= apply_calibration(1.0, result) <= 1.0
        assert 0.0 <= apply_calibration(-0.5, result) <= 1.0
        assert 0.0 <= apply_calibration(1.5, result) <= 1.0

    def test_reliability_buckets(self):
        """Reliability data should have 10 buckets with observed vs expected counts."""
        rng = np.random.RandomState(42)
        n = 100
        predicted = rng.uniform(0.1, 0.9, n)
        actual = (rng.rand(n) < predicted).astype(float)
        rows = [
            {"predicted_p_rise": float(p), "actual_rise": bool(a)}
            for p, a in zip(predicted, actual)
        ]
        result = fit_product_calibration(rows, min_samples=30)
        assert result is not None
        rel = result["reliability"]
        assert len(rel) == 10
        for bucket in rel:
            assert "bin_center" in bucket
            assert "predicted_mean" in bucket
            assert "observed_rate" in bucket
            assert "count" in bucket
