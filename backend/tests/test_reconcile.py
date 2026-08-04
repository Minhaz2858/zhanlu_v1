"""Tests for value-chain coherence (spread guardrails + directional checks)."""
from __future__ import annotations

import pytest

from app.services.forecasting.reconcile import (
    CoherenceReport, check_coherence, apply_coherence,
)


class TestCheckCoherence:
    def test_no_violations_when_coherent(self):
        report = check_coherence(
            product_key="isoprene",
            forecast_values=[5500.0, 5520.0, 5540.0],
            feedstock_forecast=[600.0, 605.0, 610.0],   # naphtha
            feedstock_key="naphtha",
            spread_inversion_threshold=0.0,
        )
        assert report.spread_inverted is False
        assert report.direction_consistent is True
        assert len(report.violations) == 0

    def test_detects_spread_inversion(self):
        """child < feedstock cost -> spread_inverted=True."""
        report = check_coherence(
            product_key="cracked_c5",
            forecast_values=[500.0, 505.0, 510.0],   # below naphtha cost
            feedstock_forecast=[600.0, 605.0, 610.0],
            feedstock_key="naphtha",
            spread_inversion_threshold=0.0,
        )
        assert report.spread_inverted is True
        assert any(v["type"] == "spread_inversion" for v in report.violations)

    def test_detects_directional_mismatch(self):
        """Feedstock moved >5% but child didn't move commensurately."""
        report = check_coherence(
            product_key="isoprene",
            forecast_values=[5500.0, 5505.0, 5510.0],   # ~0% move
            feedstock_forecast=[600.0, 660.0, 720.0],   # 20% jump
            feedstock_key="naphtha",
            direction_threshold_pct=5.0,
        )
        assert report.direction_consistent is False
        assert any(v["type"] == "directional_mismatch" for v in report.violations)

    def test_no_directional_mismatch_within_threshold(self):
        """Feedstock moved <5% -> no directional mismatch."""
        report = check_coherence(
            product_key="isoprene",
            forecast_values=[5500.0, 5510.0, 5520.0],   # ~0.4% move
            feedstock_forecast=[600.0, 602.0, 604.0],   # ~0.7% move
            feedstock_key="naphtha",
            direction_threshold_pct=5.0,
        )
        assert report.direction_consistent is True

    def test_no_feedstock_skips_checks(self):
        """If feedstock_forecast is None, no checks applied."""
        report = check_coherence(
            product_key="crude_oil",
            forecast_values=[500.0, 505.0, 510.0],
            feedstock_forecast=None,
            feedstock_key=None,
        )
        assert len(report.violations) == 0
        assert report.spread_inverted is False


class TestApplyCoherence:
    def test_clamp_on_spread_inversion(self):
        forecast = [500.0, 505.0, 510.0]
        feedstock_forecast = [600.0, 605.0, 610.0]
        report = check_coherence("test", forecast, feedstock_forecast, "naphtha")
        clamped, new_report = apply_coherence(forecast, report, feedstock_forecast, min_margin=0.2)
        assert new_report.clamped is True
        # Clamped values should be feedstock * 1.2
        assert clamped[0] == pytest.approx(720.0)  # 600 * 1.2

    def test_no_clamp_when_coherent(self):
        forecast = [5500.0, 5520.0, 5540.0]
        feedstock_forecast = [600.0, 605.0, 610.0]
        report = check_coherence("test", forecast, feedstock_forecast, "naphtha")
        clamped, new_report = apply_coherence(forecast, report, feedstock_forecast, min_margin=0.2)
        assert new_report.clamped is False
        assert clamped == forecast

    def test_directional_mismatch_flagged_not_clamped(self):
        """Directional mismatches are flags, not clamps."""
        forecast = [5500.0, 5505.0, 5510.0]
        feedstock_forecast = [600.0, 660.0, 720.0]
        report = check_coherence("test", forecast, feedstock_forecast, "naphtha")
        clamped, new_report = apply_coherence(forecast, report, feedstock_forecast, min_margin=0.2)
        assert new_report.clamped is False  # directional mismatch doesn't trigger clamp
        assert clamped == forecast
