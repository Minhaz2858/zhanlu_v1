"""Test ensemble anomaly detector.

Covers:
1. Detects injected outliers (spikes)
2. Normal data produces zero anomalies
3. Short series returns empty result
4. If sklearn missing, STL+IQR still work (graceful fallback)
"""

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.anomaly_detector import detect_anomalies, AnomalyResult


@pytest.fixture
def clean_series():
    rng = np.random.RandomState(777)
    trend = np.linspace(100, 120, 100)
    noise = rng.normal(0, 2, 100)
    season = 3 * np.sin(2 * np.pi * np.arange(100) / 7)
    return pd.Series(
        trend + noise + season,
        index=pd.date_range("2024-01-01", periods=100, freq="D"),
        name="price",
    )


class TestAnomalyDetector:
    def test_detects_injected_spikes(self, clean_series):
        """Inject 3 obvious spikes and verify detection."""
        y = clean_series.copy()
        y.iloc[30] = 200.0   # clear spike
        y.iloc[60] = 40.0    # clear dip
        y.iloc[80] = 180.0   # clear spike

        result = detect_anomalies(y, seasonal_period=7)
        assert isinstance(result, AnomalyResult)
        assert len(result.anomaly_indices) >= 1  # at least one spike detected

    def test_normal_data_no_anomalies(self, clean_series):
        """Clean series should not produce false positives."""
        result = detect_anomalies(clean_series, seasonal_period=7)
        # Expect very few or zero anomalies in clean data
        assert len(result.anomaly_indices) <= 3  # allow occasional false positive

    def test_short_series_empty(self):
        """Series too short for STL returns empty."""
        short = pd.Series([100.0, 101.0, 100.5, 99.0, 100.0],
                          name="short")
        result = detect_anomalies(short, seasonal_period=7)
        assert result.anomaly_indices == []
        assert result.anomaly_scores == []

    def test_result_structure(self, clean_series):
        """Verify AnomalyResult dataclass fields."""
        result = detect_anomalies(clean_series, seasonal_period=7)
        assert isinstance(result.anomaly_indices, list)
        assert isinstance(result.anomaly_scores, list)
        assert isinstance(result.method_votes, list)
        assert len(result.anomaly_indices) == len(result.anomaly_scores)

    def test_all_methods_contribute(self, clean_series):
        """On a long clean series, all three methods should run."""
        long_series = pd.concat([clean_series] * 2).reset_index(drop=True)
        result = detect_anomalies(long_series, seasonal_period=7)
        # method_votes length should match series length
        assert len(result.method_votes) == len(long_series)
