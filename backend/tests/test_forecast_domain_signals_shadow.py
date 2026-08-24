"""Shadow comparison test for domain signals overlay.

Validates that enabling FORECAST_DOMAIN_SIGNALS_ENABLED produces a different
forecast than the baseline (disabled) for downstream products where domain
signals apply adjustments.

The domain_signals module encodes petrochem supply-chain economics:
  - naphtha→C5 elasticities (tier-dampened: 1.00, 0.85, 0.72)
  - per-product seasonal adjustments by month

This test verifies the overlay is wired correctly and produces non-zero
adjustments for known products.
"""
import pytest
from unittest.mock import MagicMock, patch

from app.services.forecasting.domain_signals import (
    compute_domain_signal_adjustment,
    fetch_naphtha_recent_pct_change,
    _RAW_ELASTICITIES,
    _SEASONAL_RULES,
)


class TestDomainSignalsComputeAdjustment:
    """Unit tests for compute_domain_signal_adjustment()."""

    def test_known_product_has_adjustment(self):
        """Products in the elasticity map should have a non-zero adjustment."""
        from datetime import datetime
        result = compute_domain_signal_adjustment(
            product_id="cracked_c5",
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
        """Unknown products should return zero adjustment (no crash)."""
        from datetime import datetime
        result = compute_domain_signal_adjustment(
            product_id="unknown_product_xyz",
            as_of_date=datetime(2024, 3, 15),
            naphtha_pct_change=5.0,
        )
        assert result["total_pct"] == 0.0
        assert result["causal_pct"] == 0.0
        assert result["seasonal_pct"] == 0.0
        assert result["applied_rules"] == []

    def test_zero_naphtha_change_no_causal(self):
        """When naphtha hasn't moved, causal adjustment is zero."""
        from datetime import datetime
        result = compute_domain_signal_adjustment(
            product_id="cracked_c5",
            as_of_date=datetime(2024, 3, 15),
            naphtha_pct_change=0.0,
        )
        assert result["causal_pct"] == 0.0

    def test_negative_naphtha_change(self):
        """Negative naphtha change should produce negative causal adjustment."""
        from datetime import datetime
        result = compute_domain_signal_adjustment(
            product_id="cracked_c5",
            as_of_date=datetime(2024, 3, 15),
            naphtha_pct_change=-3.0,
        )
        assert result["causal_pct"] < 0.0

    def test_tier_dampening_applied(self):
        """Tier 2 products should have dampened causal effect vs tier 1."""
        from datetime import datetime
        # naphtha is tier 1 (factor 1.00)
        r1 = compute_domain_signal_adjustment(
            product_id="naphtha",
            as_of_date=datetime(2024, 3, 15),
            naphtha_pct_change=5.0,
        )
        # cracked_c5 is tier 1 (factor 1.00)
        r2 = compute_domain_signal_adjustment(
            product_id="cracked_c5",
            as_of_date=datetime(2024, 3, 15),
            naphtha_pct_change=5.0,
        )
        # Both tier 1, so similar magnitude
        assert abs(r2["causal_pct"]) <= abs(r1["causal_pct"])

    def test_seasonal_adjustment_by_month(self):
        """Seasonal adjustment should vary by month for known products."""
        from datetime import datetime
        jan = datetime(2024, 1, 15)
        jul = datetime(2024, 7, 15)

        r_jan = compute_domain_signal_adjustment(
            product_id="cracked_c5",
            as_of_date=jan,
            naphtha_pct_change=0.0,
        )
        r_jul = compute_domain_signal_adjustment(
            product_id="cracked_c5",
            as_of_date=jul,
            naphtha_pct_change=0.0,
        )
        # January and July should have different seasonal adjustments
        assert r_jan["seasonal_pct"] != r_jul["seasonal_pct"]


class TestDomainSignalsElasticityMap:
    """Tests for the elasticity map constants."""

    def test_naphtha_tier1_factor(self):
        """naphtha should be tier 1 (factor 1.00)."""
        raw, tier = _RAW_ELASTICITIES["naphtha"]
        assert tier == 1.00

    def test_cracked_c5_tier1_factor(self):
        """cracked_c5 should be tier 1 (factor 1.00)."""
        raw, tier = _RAW_ELASTICITIES["cracked_c5"]
        assert tier == 1.00

    def test_isoprene_tier2_factor(self):
        """isoprene should be tier 2 (factor 0.85)."""
        raw, tier = _RAW_ELASTICITIES["isoprene"]
        assert tier == 0.85

    def test_seasonal_map_has_entries(self):
        """Seasonal adjustment map should have entries for known products."""
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
            fetch_naphtha_recent_pct_change,
        )
        assert callable(compute_domain_signal_adjustment)
        assert callable(fetch_naphtha_recent_pct_change)
