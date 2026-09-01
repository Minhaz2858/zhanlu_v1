"""Tests for accuracy_report module: threshold checking, discrepancy reports."""
from __future__ import annotations

import pytest


class TestAccuracyThreshold:
    def test_import(self):
        """Module should be importable."""
        from app.services.forecasting.accuracy_report import AccuracyThreshold
        assert AccuracyThreshold is not None

    def test_default_thresholds(self):
        """Default thresholds: excellent=8, acceptable=15, critical=25."""
        from app.services.forecasting.accuracy_report import AccuracyThreshold
        t = AccuracyThreshold()
        assert t.excellent == 8.0
        assert t.acceptable == 15.0
        assert t.critical == 25.0

    def test_check_excellent(self):
        """MAPE < excellent threshold returns 'excellent'."""
        from app.services.forecasting.accuracy_report import AccuracyThreshold
        t = AccuracyThreshold()
        assert t.check(5.0) == "excellent"
        assert t.check(7.9) == "excellent"

    def test_check_acceptable(self):
        """MAPE between excellent and acceptable returns 'acceptable'."""
        from app.services.forecasting.accuracy_report import AccuracyThreshold
        t = AccuracyThreshold()
        assert t.check(10.0) == "acceptable"
        assert t.check(14.9) == "acceptable"

    def test_check_critical(self):
        """MAPE between acceptable and critical returns 'critical'."""
        from app.services.forecasting.accuracy_report import AccuracyThreshold
        t = AccuracyThreshold()
        assert t.check(18.0) == "critical"
        assert t.check(24.9) == "critical"

    def test_check_blocked(self):
        """MAPE >= critical returns 'blocked'."""
        from app.services.forecasting.accuracy_report import AccuracyThreshold
        t = AccuracyThreshold()
        assert t.check(25.1) == "blocked"
        assert t.check(50.0) == "blocked"

    def test_check_none_returns_unknown(self):
        """None MAPE returns 'unknown'."""
        from app.services.forecasting.accuracy_report import AccuracyThreshold
        t = AccuracyThreshold()
        assert t.check(None) == "unknown"

    def test_custom_thresholds(self):
        """Custom thresholds from constructor."""
        from app.services.forecasting.accuracy_report import AccuracyThreshold
        t = AccuracyThreshold(excellent=5, acceptable=10, critical=20)
        assert t.check(3.0) == "excellent"
        assert t.check(7.0) == "acceptable"
        assert t.check(15.0) == "critical"
        assert t.check(25.0) == "blocked"

    def test_check_thresholds_function(self):
        """check_thresholds() returns per-product status dict."""
        from app.services.forecasting.accuracy_report import (
            check_thresholds,
            AccuracyThreshold,
        )
        t = AccuracyThreshold()
        results = check_thresholds(
            {"product_a": 5.0, "product_b": 12.0, "product_c": 30.0},
            threshold=t,
        )
        assert results["product_a"]["status"] == "excellent"
        assert results["product_b"]["status"] == "acceptable"
        assert results["product_c"]["status"] == "blocked"

    def test_check_thresholds_empty_input(self):
        """Empty input returns empty dict."""
        from app.services.forecasting.accuracy_report import check_thresholds
        assert check_thresholds({}) == {}


class TestDiscrepancyReport:
    def test_build_day_level_comparison(self):
        """build_day_level should return DataFrame with required columns."""
        import pandas as pd
        from app.services.forecasting.accuracy_report import _build_day_level_comparison

        forecast_dates = [pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02"),
                          pd.Timestamp("2026-07-03")]
        forecast_values = [110.0, 190.0, 300.0]
        actual = pd.Series([100.0, 200.0, 290.0],
                           index=pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03"]))

        df = _build_day_level_comparison(forecast_values, forecast_dates, actual)
        assert len(df) == 3
        assert list(df.columns) == ["date", "predicted", "actual", "residual", "pct_error"]
        assert df.iloc[0]["residual"] == 10.0   # 110 - 100
        assert df.iloc[0]["pct_error"] == 10.0  # (10/100)*100
        assert df.iloc[1]["residual"] == -10.0  # 190 - 200
        assert df.iloc[1]["pct_error"] == 5.0   # (10/200)*100
        assert df.iloc[2]["residual"] == 10.0   # 300 - 290
        assert df.iloc[2]["pct_error"] == pytest.approx(3.448, abs=0.01)

    def test_build_day_level_no_actuals(self):
        """Returns empty DataFrame when no actuals available."""
        from app.services.forecasting.accuracy_report import _build_day_level_comparison
        import pandas as pd

        dates = [pd.Timestamp("2026-07-01")]
        df = _build_day_level_comparison([110.0], dates, None)
        assert len(df) == 0

    def test_generate_root_cause_hypotheses(self):
        """Should return hypotheses list with pattern detection."""
        import pandas as pd
        from app.services.forecasting.accuracy_report import _generate_root_cause_hypotheses

        df = pd.DataFrame({
            "date": pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03",
                                     "2026-07-04", "2026-07-05", "2026-07-06",
                                     "2026-07-07"]),
            "predicted": [100, 102, 104, 106, 108, 110, 112],
            "actual":    [100, 102, 104, 106, 108, 150, 152],  # spike at day 5-6
            "residual":  [0, 0, 0, 0, 0, -40, -40],
            "pct_error": [0, 0, 0, 0, 0, 26.7, 26.3],
        })

        hypotheses = _generate_root_cause_hypotheses(df, 7)
        assert isinstance(hypotheses, list)
        assert len(hypotheses) > 0
        # Should detect the large residual pattern
        texts = " ".join(hypotheses).lower()
        assert "spike" in texts or "large" in texts or "error" in texts

    def test_generate_root_cause_no_issues(self):
        """When MAPE is good, should return benign hypothesis."""
        import pandas as pd
        from app.services.forecasting.accuracy_report import _generate_root_cause_hypotheses

        df = pd.DataFrame({
            "date": pd.to_datetime(["2026-07-01", "2026-07-02"]),
            "predicted": [100, 102],
            "actual":    [101, 103],
            "residual":  [-1, -1],
            "pct_error": [1.0, 1.0],
        })

        hypotheses = _generate_root_cause_hypotheses(df, 7)
        assert isinstance(hypotheses, list)
        assert len(hypotheses) > 0
        # Should not flag major issues
        assert not any("critical" in h.lower() or "spike" in h.lower()
                       for h in hypotheses)
