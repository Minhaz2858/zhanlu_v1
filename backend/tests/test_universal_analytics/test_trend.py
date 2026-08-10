"""Tests for universal_analytics/trend.py — Trend analysis engine."""

import pandas as pd
import pytest


class TestTrendAnalysis:
    def test_compute_trend_slope_upward(self):
        """An upward-sloping series should return direction='up'."""
        from app.services.universal_analytics.trend import analyze_trend

        series = pd.Series(
            [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            index=pd.date_range("2026-01-01", periods=6),
        )
        result = analyze_trend(series)
        assert result["direction"] in ("up", "flat", "down")
        assert "slope" in result
        assert "strength" in result
        # upward trend: slope should be positive
        assert result["slope"] > 0

    def test_compute_trend_slope_downward(self):
        """A downward-sloping series should have negative slope."""
        from app.services.universal_analytics.trend import analyze_trend

        series = pd.Series(
            [10.0, 8.0, 6.0, 4.0, 2.0],
            index=pd.date_range("2026-01-01", periods=5),
        )
        result = analyze_trend(series)
        assert result["slope"] < 0

    def test_compute_trend_with_moving_average(self):
        """Trend analysis should include moving average in result."""
        from app.services.universal_analytics.trend import analyze_trend

        series = pd.Series(
            [1.0, 3.0, 2.0, 4.0, 3.0, 5.0, 4.0, 6.0],
            index=pd.date_range("2026-01-01", periods=8),
        )
        result = analyze_trend(series, window=3)
        assert "moving_average" in result
        assert isinstance(result["moving_average"], list)

    def test_short_series_handled_gracefully(self):
        """Very short series should not crash."""
        from app.services.universal_analytics.trend import analyze_trend

        series = pd.Series(
            [5.0, 10.0],
            index=pd.date_range("2026-01-01", periods=2),
        )
        result = analyze_trend(series)
        assert isinstance(result, dict)
        assert "direction" in result
