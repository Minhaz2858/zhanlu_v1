"""Tests for Wave 1: threshold_calibrator + configurable decision_engine thresholds."""
from __future__ import annotations

import os
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.services.forecasting.features.threshold_calibrator import (
    calibrate_thresholds,
    CalibrationReport,
)


# ------------------------------------------------------------------ #
# Helpers — build mock log objects
# ------------------------------------------------------------------ #

def _mock_log(action, actual_t, actual_th, p_rise, change_pct, roi_pct, days_ago=5):
    """Create a mock ForecastDecisionLog with all required attributes."""
    log = MagicMock()
    log.action = action
    log.actual_price_t = actual_t
    log.actual_price_th = actual_th
    log.predicted_p_rise = p_rise
    log.predicted_change_pct = change_pct
    log.roi_pct = roi_pct
    log.as_of_date = date.today() - timedelta(days=days_ago)
    log.product_id = "TN450"
    return log


def _mock_db(logs):
    """Fake DB session returning given logs."""
    db = MagicMock()
    query = MagicMock()
    db.query.return_value = query
    query.filter.return_value = query
    query.filter.return_value = query  # chained filter
    query.all.return_value = logs
    return db


# ------------------------------------------------------------------ #
# Tests
# ------------------------------------------------------------------ #

class TestCalibrateThresholds:
    """Unit tests for calibrate_thresholds()."""

    def test_insufficient_data_returns_warning(self):
        """< 10 logs → no calibration, just a warning."""
        logs = [_mock_log("buy", 100, 110, 0.75, 0.05, 5.0, days_ago=i)
                for i in range(5)]
        db = _mock_db(logs)
        report = calibrate_thresholds(db, product_key="TN450", days=90)
        assert report.sample_size == 5
        assert not report.safety_checks["min_samples"]
        assert len(report.warnings) > 0
        assert "Insufficient data" in report.warnings[0]
        assert report.recommendation is None

    def test_sufficient_data_runs_grid_search(self):
        """≥ 10 logs → grid_search is called, top results returned."""
        logs = [
            _mock_log("buy", 100, 110, 0.75, 0.05, 5.0, days_ago=i % 10)
            for i in range(15)
        ]
        db = _mock_db(logs)

        with patch(
            "app.services.forecasting.features.threshold_calibrator.grid_search_thresholds"
        ) as mock_gs:
            mock_gs.return_value = [
                {"buy_threshold": 0.72, "sell_threshold": 0.31,
                 "roi_pct": 8.2, "grid_position": "buy=3,sell=2"},
                {"buy_threshold": 0.70, "sell_threshold": 0.30,
                 "roi_pct": 6.5, "grid_position": "buy=2,sell=1"},
                {"buy_threshold": 0.75, "sell_threshold": 0.28,
                 "roi_pct": 5.1, "grid_position": "buy=4,sell=2"},
            ]
            report = calibrate_thresholds(db, product_key="TN450", days=90)

        assert report.sample_size == 15
        assert report.safety_checks["min_samples"] is True
        assert len(report.top_results) == 3
        assert report.top_results[0]["roi_pct"] == 8.2
        assert report.recommendation is not None
        assert report.recommendation["buy_threshold"] == 0.72
        assert report.recommendation["sell_threshold"] == 0.31
        assert "env_override" in report.recommendation

    def test_gap_check_fails_when_too_narrow(self):
        """Buy - sell < 0.15 → gap_ok=False, no recommendation."""
        logs = [_mock_log("buy", 100, 110, 0.75, 0.05, 5.0)
                for _ in range(15)]
        db = _mock_db(logs)

        with patch(
            "app.services.forecasting.features.threshold_calibrator.grid_search_thresholds"
        ) as mock_gs:
            mock_gs.return_value = [
                {"buy_threshold": 0.45, "sell_threshold": 0.35,
                 "roi_pct": 10.0, "grid_position": "buy=0,sell=5"},
            ]
            report = calibrate_thresholds(db, product_key="TN450", days=90)

        assert report.safety_checks["gap_ok"] is False
        assert len(report.warnings) > 0
        any_gap = any("gap" in w.lower() for w in report.warnings)
        assert any_gap, f"Expected gap warning, got: {report.warnings}"

    def test_roi_negative_no_recommendation(self):
        """Best ROI ≤ 0 → roi_positive=False, recommendation is None."""
        logs = [_mock_log("buy", 100, 95, 0.75, 0.05, -2.0)
                for _ in range(15)]
        db = _mock_db(logs)

        with patch(
            "app.services.forecasting.features.threshold_calibrator.grid_search_thresholds"
        ) as mock_gs:
            mock_gs.return_value = [
                {"buy_threshold": 0.70, "sell_threshold": 0.30,
                 "roi_pct": -5.0, "grid_position": "buy=2,sell=1"},
            ]
            report = calibrate_thresholds(db, product_key="TN450", days=90)

        assert report.safety_checks["roi_positive"] is False
        assert report.recommendation is None

    def test_product_key_filter(self):
        """product_key filter limits which logs are used."""
        # TN450 logs
        tn_logs = [_mock_log("buy", 100, 110, 0.75, 0.05, 5.0)
                   for _ in range(12)]
        tn_logs[0].product_id = "TN450"
        # bcf logs (should be excluded if filtering for TN450)
        bcf_log = _mock_log("sell", 90, 85, 0.25, -0.05, 3.0)

        db = MagicMock()
        query = MagicMock()
        db.query.return_value = query
        query.filter.return_value = query
        # Mock so when product filter is applied, it returns only TN450 logs
        query.all.return_value = tn_logs

        with patch(
            "app.services.forecasting.features.threshold_calibrator.grid_search_thresholds"
        ) as mock_gs:
            mock_gs.return_value = [
                {"buy_threshold": 0.72, "sell_threshold": 0.32,
                 "roi_pct": 7.0, "grid_position": "buy=3,sell=2"},
            ]
            report = calibrate_thresholds(db, product_key="TN450", days=90)

        assert report.sample_size == 12
        assert report.product_key == "TN450"

    def test_empty_grid_returns_warning(self):
        """grid_search returns [] → warning, no crash."""
        logs = [_mock_log("buy", 100, 110, 0.75, 0.05, 5.0)
                for _ in range(15)]
        db = _mock_db(logs)

        with patch(
            "app.services.forecasting.features.threshold_calibrator.grid_search_thresholds"
        ) as mock_gs:
            mock_gs.return_value = []
            report = calibrate_thresholds(db, product_key="TN450", days=90)

        assert len(report.top_results) == 0
        assert report.recommendation is None
        assert any("no results" in w.lower() for w in report.warnings)


class TestDecisionEngineEnvThresholds:
    """decision_engine.reads thresholds from environment variables."""

    @pytest.fixture(autouse=True)
    def save_restore_env(self):
        saved = {}
        for k in (
            "FORECAST_BUY_THRESHOLD", "FORECAST_SELL_THRESHOLD",
            "FORECAST_BUY_MIN_CHANGE", "FORECAST_SELL_MIN_CHANGE",
            "FORECAST_EDGE_THRESHOLD", "FORECAST_P_HIGH_MARGIN",
        ):
            saved[k] = os.environ.pop(k, None)
            if k in os.environ:
                del os.environ[k]
        yield
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            elif k in os.environ:
                del os.environ[k]

    def test_default_thresholds(self):
        """Without env vars, default values are used."""
        # Reload the module to pick up defaults
        import importlib
        from app.services.forecasting import decision_engine
        importlib.reload(decision_engine)
        assert decision_engine._BUY_THRESHOLD == 0.70
        assert decision_engine._SELL_THRESHOLD == 0.30
        assert decision_engine._EDGE_THRESHOLD == 0.55

    def test_env_override_thresholds(self):
        """Env vars override hardcoded defaults."""
        os.environ["FORECAST_BUY_THRESHOLD"] = "0.75"
        os.environ["FORECAST_SELL_THRESHOLD"] = "0.25"
        os.environ["FORECAST_EDGE_THRESHOLD"] = "0.60"
        os.environ["FORECAST_P_HIGH_MARGIN"] = "0.30"

        import importlib
        from app.services.forecasting import decision_engine
        importlib.reload(decision_engine)
        assert decision_engine._BUY_THRESHOLD == 0.75
        assert decision_engine._SELL_THRESHOLD == 0.25
        assert decision_engine._EDGE_THRESHOLD == 0.60
        assert decision_engine._P_HIGH_MARGIN == 0.30
