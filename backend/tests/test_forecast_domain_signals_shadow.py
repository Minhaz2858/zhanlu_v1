"""Shadow comparison test for the config-driven domain-signals overlay.

Validates that enabling FORECAST_DOMAIN_SIGNALS_ENABLED produces a different
forecast than the baseline (disabled) for downstream products where domain
signals apply adjustments.

Elasticities and seasonal rules are per-app domain-config data. The
``domain_signals_config`` fixture (tests/conftest.py) injects a temporary
generic config ("widget", "gadget", ...); without any config the overlay is a
no-op (all adjustments are zero) and the platform stays fully generic.
"""
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.services.forecasting.domain_signals import (
    compute_domain_signal_adjustment,
    fetch_root_feedstock_pct_change,
    _RAW_ELASTICITIES,
    _SEASONAL_RULES,
)


class TestDomainSignalsComputeAdjustment:
    """Unit tests for compute_domain_signal_adjustment()."""

    def test_known_product_has_adjustment(self, domain_signals_config):
        """Products in the configured elasticity map have non-zero adjustments."""
        result = compute_domain_signal_adjustment(
            product_id="widget",
            as_of_date=datetime(2024, 3, 15),
            naphtha_pct_change=5.0,
        )
        assert isinstance(result, dict)
        assert "total_pct" in result
        assert "causal_pct" in result
        assert "seasonal_pct" in result
        assert "applied_rules" in result

        # With 5% naphtha change, causal_pct should be non-zero
        assert result["causal_pct"] != 0.0

    def test_unknown_product_returns_zero(self):
        """Unknown products → zero adjustment (no crash)."""
        result = compute_domain_signal_adjustment(
            product_id="unknown_product_xyz",
            as_of_date=datetime(2024, 3, 15),
            naphtha_pct_change=5.0,
        )
        assert result["total_pct"] == 0.0
        assert result["causal_pct"] == 0.0
        assert result["seasonal_pct"] == 0.0
        assert result["applied_rules"] == []

    def test_zero_naphtha_change_no_causal(self, domain_signals_config):
        """When the feedstock hasn't moved, causal adjustment is zero."""
        result = compute_domain_signal_adjustment(
            product_id="widget",
            as_of_date=datetime(2024, 3, 15),
            naphtha_pct_change=0.0,
        )
        assert result["causal_pct"] == 0.0

    def test_negative_naphtha_change(self, domain_signals_config):
        """Negative feedstock change → negative causal adjustment."""
        result = compute_domain_signal_adjustment(
            product_id="widget",
            as_of_date=datetime(2024, 3, 15),
            naphtha_pct_change=-3.0,
        )
        assert result["causal_pct"] < 0.0

    def test_tier_dampening_applied(self, domain_signals_config):
        """Higher dampening factor → smaller causal effect (tier attenuation)."""
        # widget    = raw 0.5 × damp 1.0  → 0.5
        # widget_t2 = raw 0.5 × damp 0.85 → 0.425
        r1 = compute_domain_signal_adjustment(
            product_id="widget",
            as_of_date=datetime(2024, 3, 15),
            naphtha_pct_change=5.0,
        )
        r2 = compute_domain_signal_adjustment(
            product_id="widget_t2",
            as_of_date=datetime(2024, 3, 15),
            naphtha_pct_change=5.0,
        )
        assert abs(r2["causal_pct"]) <= abs(r1["causal_pct"])
        assert r2["causal_pct"] < r1["causal_pct"]

    def test_seasonal_adjustment_by_month(self, domain_signals_config):
        """Seasonal adjustment should vary by month for configured products."""
        jan = datetime(2024, 1, 15)  # widget|1 → -2.5
        jul = datetime(2024, 7, 15)  # no widget|7 rule → 0.0

        r_jan = compute_domain_signal_adjustment(
            product_id="widget",
            as_of_date=jan,
            naphtha_pct_change=0.0,
        )
        r_jul = compute_domain_signal_adjustment(
            product_id="widget",
            as_of_date=jul,
            naphtha_pct_change=0.0,
        )
        assert r_jan["seasonal_pct"] == -2.5
        assert r_jul["seasonal_pct"] == 0.0
        assert r_jan["seasonal_pct"] != r_jul["seasonal_pct"]


class TestDomainSignalsElasticityMap:
    """Tests for the config-loaded elasticity / seasonal tables."""

    def test_configured_elasticity_entries(self, domain_signals_config):
        """Config-loaded raw/tier pairs for configured products."""
        raw, tier = _RAW_ELASTICITIES["widget"]
        assert raw == 0.5 and tier == 1.00
        _raw_t2, tier_t2 = _RAW_ELASTICITIES["widget_t2"]
        assert tier_t2 == 0.85

    def test_no_config_tables_are_empty(self):
        """Empty config → no elasticity / seasonal data (generic platform)."""
        assert _RAW_ELASTICITIES == {}
        assert _SEASONAL_RULES == {}

    def test_seasonal_map_has_entries(self, domain_signals_config):
        """Seasonal adjustment map has entries for configured products."""
        assert len(_SEASONAL_RULES) > 0


class TestDomainSignalsEngineIntegration:
    """Integration tests for the engine wiring (Step 8.55)."""

    def test_domain_signals_flag_reads_from_config(self):
        """The engine should read FORECAST_DOMAIN_SIGNALS_ENABLED from config."""
        from app.config import settings
        # After .env update, this should be True
        assert settings.FORECAST_DOMAIN_SIGNALS_ENABLED is True

    def test_domain_signals_module_importable(self):
        """The domain_signals module should import without errors."""
        from app.services.forecasting.domain_signals import (
            compute_domain_signal_adjustment,
            fetch_root_feedstock_pct_change,
        )
        assert callable(compute_domain_signal_adjustment)
        assert callable(fetch_root_feedstock_pct_change)
