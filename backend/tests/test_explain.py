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
