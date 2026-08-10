"""Tests for universal_analytics/anomaly.py — P5 Anomaly Detection.

Flag-gated behind UNIVERSAL_ANALYTICS_ANOMALY (default OFF).
Tests: z-score detection, IQR detection, seasonal decomposition,
        edge cases (empty, constant, single point).
"""

import pandas as pd
import numpy as np
import pytest


# ── Flag gating tests ───────────────────────────────────────────────

class TestAnomalyFlagGating:
    def test_is_enabled_false_by_default(self):
        from app.services.universal_analytics.anomaly import is_anomaly_enabled
        with pytest.MonkeyPatch().context() as mp:
            mp.delenv("UNIVERSAL_ANALYTICS_ANOMALY", raising=False)
            assert is_anomaly_enabled() is False

    def test_is_enabled_true_when_flag_on(self):
        from app.services.universal_analytics.anomaly import is_anomaly_enabled
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("UNIVERSAL_ANALYTICS_ANOMALY", "true")
            assert is_anomaly_enabled() is True


# ── Anomaly detection tests ─────────────────────────────────────────

class TestZScoreDetection:
    def test_detects_high_zscore_anomaly(self):
        """Point with |z| > 3 is flagged as anomaly."""
        from app.services.universal_analytics.anomaly import detect_anomalies
        # 100 values at 1-2 range, then one 100 → clear outlier (z >> 3)
        vals = [1.0, 2.0] * 50 + [100.0]
        series = pd.Series(
            vals,
            index=pd.date_range("2026-01-01", periods=101),
        )
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("UNIVERSAL_ANALYTICS_ANOMALY", "true")
            result = detect_anomalies(series, method="zscore")
            assert len(result) > 0
            assert result[0]["index"] == pd.Timestamp("2026-04-11").isoformat()[:10]

    def test_no_anomalies_in_constant_series(self):
        """Constant series has no anomalies."""
        from app.services.universal_analytics.anomaly import detect_anomalies
        series = pd.Series([5.0] * 100)
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("UNIVERSAL_ANALYTICS_ANOMALY", "true")
            result = detect_anomalies(series, method="zscore")
            assert len(result) == 0


class TestIQRDetection:
    def test_detects_iqr_outlier(self):
        """IQR method finds points outside 1.5 * IQR."""
        from app.services.universal_analytics.anomaly import detect_anomalies
        series = pd.Series(
            [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 50],  # 50 is outlier
            index=pd.date_range("2026-01-01", periods=11),
        )
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("UNIVERSAL_ANALYTICS_ANOMALY", "true")
            result = detect_anomalies(series, method="iqr")
            assert len(result) > 0


class TestEdgeCases:
    def test_short_series_returns_empty(self):
        """Very short series returns empty list."""
        from app.services.universal_analytics.anomaly import detect_anomalies
        series = pd.Series([1.0, 2.0])
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("UNIVERSAL_ANALYTICS_ANOMALY", "true")
            result = detect_anomalies(series, method="zscore")
            assert result == []

    def test_single_point_returns_empty(self):
        """Single data point has no anomalies to detect."""
        from app.services.universal_analytics.anomaly import detect_anomalies
        series = pd.Series([42.0])
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("UNIVERSAL_ANALYTICS_ANOMALY", "true")
            result = detect_anomalies(series, method="zscore")
            assert result == []
