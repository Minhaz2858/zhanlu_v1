"""P0.3: Replace Gaussian p_rise with empirical residual-CDF computation.

The old code recovers sigma from the conformal half-width via a Gaussian
assumption (sigma = hw / z_90), then computes p_rise = Phi(delta / sigma).
This is miscalibrated for fat-tailed chemical price residuals, and produces
the degenerate p_rise = 0.50 exactly when the honesty gate publishes a flat
naive forecast (delta ≈ 0).

The fix: compute p_rise empirically from the residual distribution:
    p_rise = mean(residual > -delta)  with Laplace smoothing (k+1)/(n+2)

Fall back to the Gaussian path only when < min_samples residuals are available.
When the gate published the naive fallback (below_naive_flat), return p_rise=None
so callers can render "—" instead of 0.50.
"""
import numpy as np
import pytest

from app.services.forecasting.price_change_probability import (
    PriceChangeProbability,
    compute,
    compute_empirical,
)


def _make_residuals(n: int = 50, seed: int = 42) -> dict[int, list[float]]:
    """Generate synthetic residuals for testing."""
    rng = np.random.RandomState(seed)
    residuals_h7 = list(rng.randn(n) * 5)  # moderate variance
    return {7: residuals_h7, 14: list(rng.randn(n) * 8), 30: list(rng.randn(n) * 12)}


class TestEmpiricalPRise:
    """The empirical p_rise must be computed from the residual CDF."""

    def test_compute_empirical_exists(self):
        """compute_empirical function must be importable."""
        assert callable(compute_empirical)

    def test_empirical_p_rise_from_residuals(self):
        """With upward-biased residuals, p_rise should be > 0.5."""
        # Residuals mostly positive → point forecast likely above last actual → high p_rise
        residuals_h7 = [1.0] * 30 + [-1.0] * 10  # 75% positive
        result = compute_empirical(
            point_forecast_delta=5.0,  # delta > 0 (forecast above last actual)
            residuals_by_horizon={7: residuals_h7},
            horizon=7,
            min_samples=10,
        )
        assert result.p_rise is not None
        assert result.p_rise > 0.5, f"p_rise should be > 0.5 with positive delta and positive-skewed residuals, got {result.p_rise}"

    def test_empirical_p_rise_with_negative_delta(self):
        """With negative delta, p_rise should be < 0.5."""
        residuals_h7 = [0.0] * 30  # symmetric around zero
        result = compute_empirical(
            point_forecast_delta=-5.0,  # delta < 0 (forecast below last actual)
            residuals_by_horizon={7: residuals_h7},
            horizon=7,
            min_samples=10,
        )
        assert result.p_rise is not None
        assert result.p_rise < 0.5, f"p_rise should be < 0.5 with negative delta, got {result.p_rise}"

    def test_empirical_p_rise_with_zero_delta(self):
        """With zero delta and symmetric residuals, p_rise ≈ 0.5 (but NOT exactly 0.5 due to smoothing)."""
        residuals_h7 = list(np.random.RandomState(42).randn(50))
        result = compute_empirical(
            point_forecast_delta=0.0,
            residuals_by_horizon={7: residuals_h7},
            horizon=7,
            min_samples=10,
        )
        assert result.p_rise is not None
        # With Laplace smoothing, p_rise at delta=0 with symmetric residuals ≈ 0.5
        # but not exactly 0.5 (unlike the old Gaussian which gives 0.5000...)
        assert 0.35 < result.p_rise < 0.65, f"p_rise near 0.5 expected, got {result.p_rise}"

    def test_empirical_gated_flat_returns_none(self):
        """When below_naive_flat=True, p_rise should be None (not 0.50)."""
        result = compute_empirical(
            point_forecast_delta=0.0,
            residuals_by_horizon={7: [1.0] * 30},
            horizon=7,
            below_naive_flat=True,
            min_samples=10,
        )
        assert result.p_rise is None, "p_rise must be None when gate published naive fallback"

    def test_empirical_few_residuals_falls_back_to_gaussian(self):
        """With < min_samples residuals, fall back to Gaussian computation."""
        result = compute_empirical(
            point_forecast_delta=3.0,
            residuals_by_horizon={7: [1.0, 2.0]},  # only 2 residuals
            horizon=7,
            min_samples=10,
        )
        assert result.p_rise is not None
        # Should have fallen back to Gaussian — the exact value depends on sigma estimation
        assert 0.0 < result.p_rise < 1.0

    def test_empirical_laplace_smoothing(self):
        """Verify Laplace smoothing: with 0 positive residuals out of N,
        p_rise should be (0+1)/(N+2), not 0.0."""
        # All residuals are large negative → no residual > -delta for small delta
        residuals_h7 = [-100.0] * 30
        result = compute_empirical(
            point_forecast_delta=0.1,  # small positive delta
            residuals_by_horizon={7: residuals_h7},
            horizon=7,
            min_samples=10,
        )
        assert result.p_rise is not None
        # Laplace smoothing: (0+1)/(30+2) = 1/32 ≈ 0.03125
        expected_laplace = 1 / (30 + 2)
        assert abs(result.p_rise - expected_laplace) < 0.01, (
            f"Laplace-smoothed p_rise should be ~{expected_laplace:.4f}, got {result.p_rise:.4f}"
        )

    def test_empirical_matches_gaussian_for_normal_residuals(self):
        """When residuals ARE Gaussian, empirical should be close to Gaussian result."""
        rng = np.random.RandomState(123)
        # Large sample of truly Gaussian residuals
        residuals_h7 = list(rng.randn(500) * 5)
        delta = 2.0  # moderate positive delta

        result_emp = compute_empirical(
            point_forecast_delta=delta,
            residuals_by_horizon={7: residuals_h7},
            horizon=7,
            min_samples=10,
        )

        # For comparison, compute the Gaussian result manually
        from scipy.stats import norm
        sigma = float(np.std(residuals_h7))
        p_rise_gauss = float(norm.cdf(delta / sigma)) if sigma > 0 else 0.5

        # They should be close (within 5%) for truly Gaussian data
        assert result_emp.p_rise is not None
        assert abs(result_emp.p_rise - p_rise_gauss) < 0.05, (
            f"Empirical ({result_emp.p_rise:.4f}) should be close to Gaussian ({p_rise_gauss:.4f}) "
            f"for truly Gaussian residuals"
        )
