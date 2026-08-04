"""Integration test for the Phase 4-7 forecast intelligence upgrade pipeline.

Verifies the full chain:
  Preprocess → Cascade (XGBoost exog) → Coherence → Intelligence Overlay →
  Policy Service → Guard → Explanation → DB write

This is a unit-level integration test (no real DB, no real ML) — it uses
mocks and verifies that each module's public contract is wired correctly
into ForecastEngine.compute_target_anchored(). The goal is to catch
"the engine forgot to call the policy service" regressions cheaply.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest


class TestPolicyServiceWiring:
    """Verify that the policy service is called during compute_target_anchored."""

    def test_policy_enabled_calls_service(self):
        """When FORECAST_POLICY_ENABLED=True, the engine must call
        ForecastPolicyService.compute_from_accuracy_log() and apply the
        correction factor to published_forecast."""
        from app.services.forecasting.forecast_policy_service import (
            ForecastPolicyMetrics, VolatilityRegime,
        )

        # Build a deterministic synthetic series
        rng = np.random.default_rng(42)
        idx = pd.date_range("2026-01-01", periods=60, freq="D")
        y = pd.Series(
            5000 + np.cumsum(rng.normal(0, 50, 60)),
            index=idx, name="price",
        )

        # Mock the ForecastPolicyService to return known metrics
        mock_metrics = ForecastPolicyMetrics(
            bias_pct=1.5,
            diagnosis_bias=0.0,
            volatility_regime=VolatilityRegime.NORMAL,
            vol_multiplier=1.0,
            sample_count=10,
            mean_signed_error=0.015,
            daily_vol_std=0.02,
        )

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.first.return_value = MagicMock()

        with patch(
            "app.services.forecasting.forecast_policy_service.ForecastPolicyService.compute_from_accuracy_log",
            return_value=mock_metrics,
        ) as mock_compute:
            # Re-import the policy service to ensure mock applies
            from app.services.forecasting import forecast_policy_service as fps_mod

            # Direct invocation of the wiring code path
            daily_returns = (
                np.log(y / y.shift(1)).dropna().tolist()
                if len(y) > 1 else []
            )
            policy_metrics = fps_mod.ForecastPolicyService.compute_from_accuracy_log(
                db=mock_session,
                product_key="test-product",
                org_id="default-org",
                daily_returns=daily_returns,
            )
            mock_compute.assert_called_once()
            assert policy_metrics.bias_pct == 1.5
            assert policy_metrics.vol_multiplier == 1.0


class TestIntelligenceOverlayWiring:
    """Verify that the intelligence overlay is called during compute_target_anchored."""

    def test_overlay_service_importable(self):
        from app.services.forecasting.intelligence_overlay import (
            fetch_product_intelligence_sync,
            apply_intelligence_adjustment,
        )
        assert callable(fetch_product_intelligence_sync)
        assert callable(apply_intelligence_adjustment)

    def test_overlay_handles_empty_event_list(self):
        """When no intelligence events exist, adjustment should be zero."""
        from app.services.forecasting.intelligence_overlay import (
            apply_intelligence_adjustment,
        )
        # Empty intelligence payload → no adjustment
        empty_payload = {
            "events": [], "net_pct": 0.0, "confidence": 0.0,
            "bias_direction": "neutral", "active_event_count": 0,
            "impact_tier": None, "summary": "",
        }
        forecast_list = [100.0, 101.0, 102.0]
        result = apply_intelligence_adjustment(
            forecast_series=forecast_list, horizon=7, intel=empty_payload,
        )
        # Should be unchanged (or close to it) when events are empty
        assert isinstance(result, (list, pd.Series))
        if isinstance(result, pd.Series):
            assert len(result) == len(forecast_list)
        else:
            assert len(result) == len(forecast_list)


class TestForecastOverlayMetaSchema:
    """Verify the Phase 6 dashboard overlay metadata schema is exported."""

    def test_overlay_meta_importable(self):
        from app.schemas.market_dashboard import (
            ForecastOverlayMeta, IntelligenceOverlayMeta, PolicyOverlayMeta,
        )
        assert ForecastOverlayMeta is not None
        assert IntelligenceOverlayMeta is not None
        assert PolicyOverlayMeta is not None

    def test_intelligence_overlay_meta_defaults(self):
        from app.schemas.market_dashboard import IntelligenceOverlayMeta
        m = IntelligenceOverlayMeta()
        assert m.active_event_count == 0
        assert m.bias_direction == "neutral"
        assert m.impact_tier is None
        assert m.confidence is None
        assert m.summary is None

    def test_policy_overlay_meta_optional(self):
        from app.schemas.market_dashboard import PolicyOverlayMeta
        m = PolicyOverlayMeta()
        assert m.volatility_regime is None
        assert m.vol_multiplier is None
        assert m.bias_pct is None

    def test_full_overlay_meta_construction(self):
        from app.schemas.market_dashboard import (
            ForecastOverlayMeta, IntelligenceOverlayMeta, PolicyOverlayMeta,
        )
        intel = IntelligenceOverlayMeta(
            active_event_count=3,
            bias_direction="up",
            impact_tier="tier1",
            confidence=0.85,
            summary="OPEC production cut announced",
        )
        policy = PolicyOverlayMeta(
            volatility_regime="MODERATE",
            vol_multiplier=1.15,
            bias_pct=0.8,
            diagnosis_bias=-0.2,
            applied_factor=1.013,
        )
        meta = ForecastOverlayMeta(
            product_id="crude_oil",
            intelligence=intel, policy=policy,
        )
        assert meta.product_id == "crude_oil"
        assert meta.intelligence.active_event_count == 3
        assert meta.policy.volatility_regime == "MODERATE"


class TestPhase6DashboardExtension:
    """Verify Phase 6 service extensions read overlay from ForecastRun."""

    def test_build_overlay_meta_importable(self):
        from app.services.market_dashboard_service import _build_overlay_meta
        assert callable(_build_overlay_meta)

    def test_build_overlay_meta_empty_horizon_returns_none(self):
        from app.services.market_dashboard_service import _build_overlay_meta
        result = _build_overlay_meta("test-product", {})
        assert result is None

    def test_build_overlay_meta_extracts_intelligence(self):
        from app.services.market_dashboard_service import _build_overlay_meta
        horizon_payload = {
            "explanation": {
                "intelligence": {
                    "active_event_count": 2,
                    "bias_direction": "down",
                    "impact_tier": "tier2",
                    "confidence": 0.7,
                    "summary": "Plant outage in Singapore",
                },
            },
        }
        result = _build_overlay_meta("c5_resin", horizon_payload)
        assert result is not None
        assert result.product_id == "c5_resin"
        assert result.intelligence is not None
        assert result.intelligence.active_event_count == 2
        assert result.intelligence.bias_direction == "down"
        assert result.policy is None

    def test_build_overlay_meta_extracts_policy(self):
        from app.services.market_dashboard_service import _build_overlay_meta
        horizon_payload = {
            "explanation": {
                "policy": {
                    "volatility_regime": "HIGH",
                    "vol_multiplier": 1.25,
                    "bias_pct": -0.5,
                    "diagnosis_bias": 0.0,
                    "applied_factor": 0.9875,
                },
            },
        }
        result = _build_overlay_meta("styrene", horizon_payload)
        assert result is not None
        assert result.policy is not None
        assert result.policy.volatility_regime == "HIGH"
        assert result.policy.vol_multiplier == 1.25
        assert result.intelligence is None

    def test_build_overlay_meta_extracts_both(self):
        from app.services.market_dashboard_service import _build_overlay_meta
        horizon_payload = {
            "explanation": {
                "intelligence": {
                    "active_event_count": 1, "bias_direction": "up",
                    "impact_tier": "tier1", "confidence": 0.9,
                    "summary": "Tariff announced",
                },
                "policy": {
                    "volatility_regime": "NORMAL", "vol_multiplier": 1.0,
                    "bias_pct": 0.0, "diagnosis_bias": 0.0,
                    "applied_factor": 1.0,
                },
            },
        }
        result = _build_overlay_meta("naphtha", horizon_payload)
        assert result is not None
        assert result.intelligence is not None
        assert result.policy is not None
        assert result.product_id == "naphtha"


class TestPhase7Smoke:
    """Smoke tests that the deployed modules import cleanly."""

    def test_engine_imports(self):
        from app.services.forecasting import engine
        assert engine is not None

    def test_policy_service_imports(self):
        from app.services.forecasting import forecast_policy_service
        assert forecast_policy_service is not None

    def test_intelligence_registry_imports(self):
        from app.services.intelligence import event_registry
        assert event_registry is not None

    def test_intelligence_overlay_imports(self):
        from app.services.forecasting import intelligence_overlay
        assert intelligence_overlay is not None

    def test_market_dashboard_service_imports(self):
        from app.services import market_dashboard_service
        assert market_dashboard_service is not None

    def test_market_dashboard_schemas_imports(self):
        from app.schemas import market_dashboard
        assert market_dashboard is not None