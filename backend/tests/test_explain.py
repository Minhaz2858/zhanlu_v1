"""Tests for driver attribution + NL explanation."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.forecasting.explain import (
    DriverAttribution, Explanation, explain_forecast,
)


def _make_cleaning_report():
    return MagicMock(
        n_spikes_detected=2,
        spike_dates=["2026-07-15", "2026-07-22"],
        n_level_shifts=0,
        is_stale=False,
        notes="2 spikes corrected",
    )


def _make_coherence_report():
    return MagicMock(
        violations=[],
        spread_inverted=False,
        direction_consistent=True,
        clamped=False,
    )


class TestExplainForecast:
    def test_produces_nl_summary(self):
        explanation = explain_forecast(
            product_key="isoprene",
            forecast_values=[5500.0, 5520.0, 5540.0],
            previous_forecast=[5480.0, 5500.0, 5520.0],
            xgboost_model=None,
            feature_names=["cracked_c5_lag1", "naphtha_lag1"],
            cleaning_report=_make_cleaning_report(),
            coherence_report=_make_coherence_report(),
            drift_status={"is_drifting": False},
            honesty_gate_triggered=True,
        )
        assert isinstance(explanation, Explanation)
        assert explanation.summary
        assert "isoprene" in explanation.summary.lower()

    def test_confidence_high_when_no_gate_no_drift(self):
        explanation = explain_forecast(
            "isoprene", [5500.0], [5480.0], None, [],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": False}, honesty_gate_triggered=False,
        )
        assert explanation.confidence == "high"

    def test_confidence_medium_when_gate_triggered(self):
        explanation = explain_forecast(
            "isoprene", [5500.0], [5480.0], None, [],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": False}, honesty_gate_triggered=True,
        )
        assert explanation.confidence == "medium"

    def test_confidence_medium_when_drift_detected(self):
        explanation = explain_forecast(
            "isoprene", [5500.0], [5480.0], None, [],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": True}, honesty_gate_triggered=False,
        )
        assert explanation.confidence in ("medium", "low")

    def test_confidence_low_when_both(self):
        explanation = explain_forecast(
            "isoprene", [5500.0], [5480.0], None, [],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": True}, honesty_gate_triggered=True,
        )
        assert explanation.confidence == "low"

    def test_cleaning_note_in_explanation(self):
        explanation = explain_forecast(
            "isoprene", [5500.0], None, None, [],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": False}, honesty_gate_triggered=False,
        )
        assert "spike" in explanation.cleaning_note.lower()

    def test_no_shap_import(self):
        """Verify no SHAP dependency is used."""
        explanation = explain_forecast(
            "isoprene", [5500.0, 5520.0], None, None, [],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": False}, honesty_gate_triggered=False,
        )
        assert explanation.drivers == []

    def test_drivers_empty_when_no_xgboost(self):
        explanation = explain_forecast(
            "isoprene", [5500.0], None, None, [],
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": False}, honesty_gate_triggered=False,
        )
        assert len(explanation.drivers) == 0

    def test_series_object_does_not_crash(self):
        """Bug #1 regression: passing a pd.Series does NOT crash with 'Series'
        object has no attribute '_model'."""
        import pandas as pd
        series = pd.Series([1.0, 2.0, 3.0])
        explanation = explain_forecast(
            "isoprene", [5500.0, 5520.0], None,
            xgboost_model=series,  # Bug: passing Series instead of model
            feature_names=["f1", "f2"],
            cleaning_report=_make_cleaning_report(),
            coherence_report=_make_coherence_report(),
            drift_status={"is_drifting": False},
            honesty_gate_triggered=False,
        )
        assert isinstance(explanation, Explanation)
        assert len(explanation.drivers) == 0  # no crash, just empty drivers

    def test_model_with_attr_extracts_drivers(self):
        """Passing a proper model mock (with _model._feature_names) extracts drivers."""
        model_mock = MagicMock()
        model_mock._model = MagicMock()
        model_mock._model.feature_importances_ = [0.6, 0.4]
        model_mock._model.get_booster.return_value.feature_names = ["cracked_c5_lag1", "naphtha_lag1"]
        explanation = explain_forecast(
            "isoprene", [5500.0, 5520.0], None,
            xgboost_model=model_mock,
            feature_names=["cracked_c5_lag1", "naphtha_lag1"],
            cleaning_report=_make_cleaning_report(),
            coherence_report=_make_coherence_report(),
            drift_status={"is_drifting": False},
            honesty_gate_triggered=False,
        )
        assert len(explanation.drivers) >= 1
        assert explanation.drivers[0].feature in ("cracked_c5_lag1", "naphtha_lag1")

    def test_none_model_handled_gracefully(self):
        """None xgboost_model does not crash (pre-existing behavior)."""
        explanation = explain_forecast(
            "isoprene", [5500.0], None, None, None,
            _make_cleaning_report(), _make_coherence_report(),
            {"is_drifting": False}, honesty_gate_triggered=False,
        )
        assert isinstance(explanation, Explanation)
        assert len(explanation.drivers) == 0
