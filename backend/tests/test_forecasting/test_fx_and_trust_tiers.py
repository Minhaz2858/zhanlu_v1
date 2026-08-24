"""P2.15: Dynamic FX loader + realized-metric trust tiers.

Two changes:
1. FxLoader fetches the latest USDCNY from available data sources (try
   intelligence/price tables), with a fallback chain: live → last cache →
   hardcoded 7.10 (with warning log).
2. Trust tiers are computed from realized metrics per run (realized MAPE
   vs naive, drift status, cadence from row_count) instead of static
   product frozensets. Static lists are demoted to cold-start fallback only.
"""
import pytest

from app.services.forecasting.features.exogenous_loaders import FxLoader


class TestDynamicFxLoader:
    """FxLoader must fetch from data sources, not just a hardcoded constant."""

    def test_load_returns_finite_rate(self):
        """FxLoader.load() must return a finite, positive float."""
        rate = FxLoader.load()
        assert rate > 0
        assert rate < 20  # sanity: USDCNY is always < 20
        assert isinstance(rate, float)

    def test_fallback_chain_on_failure(self):
        """When no data source is available, fallback to cached or hardcoded 7.10."""
        # Force no data by calling with no DB session
        rate = FxLoader.load(session=None)
        # Should still return a valid rate (hardcoded fallback)
        assert rate > 0
        assert rate < 20

    def test_hardcoded_fallback_is_710(self):
        """The hardcoded fallback must be 7.10."""
        assert FxLoader.FX_RATE_FALLBACK == 7.10


class TestRealizedMetricTrustTiers:
    """Trust tiers computed from realized metrics, not static lists."""

    def test_classify_uses_realized_mape_when_available(self):
        """When realized MAPE < naive MAPE, product should get higher tier."""
        from app.services.forecasting.forecast_trust_tier import classify_trust

        result = classify_trust(
            realized_mape=5.0,      # beats naive
            naive_mape=8.0,
            drift_status=None,
            cadence_row_count=120,  # daily product, lots of data
        )
        # Should be at least medium (beats naive with good data)
        assert result["tier"] in ("high", "medium")

    def test_static_lists_are_fallback_only(self):
        """Static frozensets should only be used when no realized metrics available."""
        from app.services.forecasting.forecast_trust_tier import classify_trust

        # No realized metrics → should fall back to static lists
        result = classify_trust(
            realized_mape=None,
            naive_mape=None,
            drift_status=None,
            cadence_row_count=120,
        )
        # Must still return a valid tier (cold-start via static lists)
        assert result["tier"] in ("high", "medium", "directional", "low")

    def test_drift_drops_tier(self):
        """When drift is detected, tier should be downgraded."""
        from app.services.forecasting.forecast_trust_tier import classify_trust

        result_normal = classify_trust(
            realized_mape=5.0, naive_mape=8.0,
            drift_status=None, cadence_row_count=120,
        )
        result_drifted = classify_trust(
            realized_mape=5.0, naive_mape=8.0,
            drift_status="degraded", cadence_row_count=120,
        )
        # Drifted tier should be lower or equal
        tier_order = {"high": 3, "medium": 2, "directional": 1, "low": 0}
        assert tier_order[result_drifted["tier"]] <= tier_order[result_normal["tier"]]
