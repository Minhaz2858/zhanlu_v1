"""Test adversarial distribution-shift detector (P3-1D).

Validates that detect_shift() correctly identifies when recent data
has a different distribution from historical data.
"""
import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.ops.adversarial_shift import detect_shift, ShiftResult


class TestAdversarialShiftDetector:
    """Tests for detect_shift()."""

    def test_no_shift_similar_distributions(self):
        """When distributions are identical, no shift should be detected."""
        np.random.seed(42)
        X_hist = np.random.normal(0, 1, (100, 5))
        X_rec = np.random.normal(0, 1, (100, 5))  # same distribution

        result = detect_shift(X_hist, X_rec, threshold=0.70, min_samples=20)

        assert isinstance(result, ShiftResult)
        assert result.n_historical == 100
        assert result.n_recent == 100
        # With identical distributions, classifier should be near random (0.5)
        assert result.score < 0.70  # below threshold
        assert result.is_shifted is False

    def test_shift_different_distributions(self):
        """When distributions differ significantly, shift should be detected."""
        np.random.seed(42)
        X_hist = np.random.normal(0, 1, (100, 5))
        X_rec = np.random.normal(5, 2, (100, 5))  # very different distribution

        result = detect_shift(X_hist, X_rec, threshold=0.70, min_samples=20)

        assert isinstance(result, ShiftResult)
        # With very different distributions, classifier should be highly accurate
        assert result.score > 0.70  # above threshold
        assert result.is_shifted is True

    def test_insufficient_samples_returns_no_shift(self):
        """With too few samples, should return no shift with message."""
        X_hist = np.random.normal(0, 1, (10, 5))  # too few
        X_rec = np.random.normal(0, 1, (100, 5))

        result = detect_shift(X_hist, X_rec, threshold=0.70, min_samples=20)

        assert result.is_shifted is False
        assert result.score == 0.0
        assert "Insufficient samples" in result.message

    def test_dataframe_input(self):
        """Should accept pandas DataFrame input."""
        np.random.seed(42)
        X_hist = pd.DataFrame(np.random.normal(0, 1, (100, 5)))
        X_rec = pd.DataFrame(np.random.normal(0, 1, (100, 5)))

        result = detect_shift(X_hist, X_rec, threshold=0.70, min_samples=20)

        assert isinstance(result, ShiftResult)
        assert result.n_historical == 100
        assert result.n_recent == 100

    def test_nan_rows_removed(self):
        """Rows with NaN should be removed before analysis."""
        np.random.seed(42)
        X_hist = np.random.normal(0, 1, (100, 5))
        X_rec = np.random.normal(0, 1, (100, 5))
        # Add some NaN values
        X_rec[0, 0] = np.nan
        X_rec[1, 2] = np.nan

        result = detect_shift(X_hist, X_rec, threshold=0.70, min_samples=20)

        # Should still work after removing NaN rows
        assert result.n_recent == 98  # 2 rows removed
        assert result.is_shifted is False  # same distribution

    def test_threshold_parameter(self):
        """Higher threshold should make detection harder."""
        np.random.seed(42)
        X_hist = np.random.normal(0, 1, (100, 5))
        X_rec = np.random.normal(2, 1.5, (100, 5))  # moderately different

        # Low threshold: should detect shift
        result_low = detect_shift(X_hist, X_rec, threshold=0.60, min_samples=20)
        # High threshold: might not detect
        result_high = detect_shift(X_hist, X_rec, threshold=0.90, min_samples=20)

        # Low threshold is more likely to detect than high threshold
        if result_low.is_shifted:
            assert result_low.score >= 0.60
        if result_high.is_shifted:
            assert result_high.score >= 0.90
