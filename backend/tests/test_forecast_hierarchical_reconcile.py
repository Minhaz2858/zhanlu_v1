"""Test hierarchical reconciliation: top-down allocation + middle-out constraint.

Covers:
- allocate_top_down() with/without child forecasts
- Blend ratio behavior
- constrain_middle_out() with parent ceiling and sibling ordering
"""
import pytest
import numpy as np

from app.services.forecasting.reconcile import (
    allocate_top_down,
    constrain_middle_out,
    TopDownAllocationResult,
    MiddleOutConstraintResult,
    _DEFAULT_YIELD_RATIOS,
    _DEFAULT_MARGIN_PREMIUMS,
)


# ---------------------------------------------------------------------------
# Top-down allocation
# ---------------------------------------------------------------------------

class TestTopDownAllocation:
    """Tests for allocate_top_down()."""

    def test_full_top_down_no_child_forecasts(self):
        """When no child forecasts, allocation uses implied forecast (parent * 1+margin)."""
        parent_forecast = [100.0, 105.0, 110.0]
        result = allocate_top_down("naphtha", parent_forecast)

        assert isinstance(result, TopDownAllocationResult)
        assert result.parent_key == "naphtha"
        assert len(result.child_allocations) > 0

        # C5 cracked margin is 0.20 → implied = parent * 1.20
        c5_forecast = result.child_allocations.get("c5_cracked")
        assert c5_forecast is not None
        assert len(c5_forecast) == 3
        assert c5_forecast[0] == pytest.approx(120.0, abs=0.1)  # 100 * 1.20

    def test_blend_with_child_forecasts(self):
        """With child forecasts, result is blended between own and implied."""
        parent_forecast = [100.0, 105.0, 110.0]
        child_forecasts = {
            "c5_cracked": [130.0, 135.0, 140.0],  # higher than implied
        }

        result = allocate_top_down("naphtha", parent_forecast, child_forecasts, blend_ratio=0.5)
        c5 = result.child_allocations["c5_cracked"]

        # implied[0] = 100 * 1.20 = 120
        # own[0] = 130
        # blended[0] = 0.5 * 120 + 0.5 * 130 = 125
        assert c5[0] == pytest.approx(125.0, abs=0.1)

    def test_blend_ratio_0_uses_own_forecast(self):
        """blend_ratio=0.0 means use only the child's own forecast."""
        parent_forecast = [100.0]
        child_forecasts = {"c5_cracked": [150.0]}

        result = allocate_top_down("naphtha", parent_forecast, child_forecasts, blend_ratio=0.0)
        assert result.child_allocations["c5_cracked"][0] == pytest.approx(150.0, abs=0.1)

    def test_blend_ratio_1_uses_implied_forecast(self):
        """blend_ratio=1.0 means use only the parent-implied forecast."""
        parent_forecast = [100.0]
        child_forecasts = {"c5_cracked": [150.0]}

        result = allocate_top_down("naphtha", parent_forecast, child_forecasts, blend_ratio=1.0)
        # implied = 100 * 1.20 = 120
        assert result.child_allocations["c5_cracked"][0] == pytest.approx(120.0, abs=0.1)

    def test_length_mismatch_uses_implied(self):
        """If child forecast length != parent length, use implied only."""
        parent_forecast = [100.0, 105.0, 110.0]
        child_forecasts = {"c5_cracked": [130.0, 135.0]}  # length 2 vs 3

        result = allocate_top_down("naphtha", parent_forecast, child_forecasts)
        # Should use implied (120, 126, 132)
        assert len(result.child_allocations["c5_cracked"]) == 3
        assert result.child_allocations["c5_cracked"][0] == pytest.approx(120.0, abs=0.1)

        # Should record adjustment
        assert any(a["type"] == "length_mismatch" for a in result.adjustments_made)

    def test_custom_yield_ratios(self):
        """Custom yield ratios override defaults."""
        parent_forecast = [100.0]
        custom_ratios = {"custom_product": 0.10}
        custom_margins = {"custom_product": 0.30}

        result = allocate_top_down(
            "naphtha", parent_forecast,
            yield_ratios=custom_ratios,
            margin_premiums=custom_margins,
        )
        assert "custom_product" in result.child_allocations
        # implied = 100 * (1 + 0.30) = 130
        assert result.child_allocations["custom_product"][0] == pytest.approx(130.0, abs=0.1)

    def test_no_yield_ratios_returns_empty(self):
        """If no yield ratios for the parent, return empty allocations."""
        result = allocate_top_down("unknown_parent", [100.0])
        assert result.child_allocations == {}
        assert result.yield_ratios_used == {}

    def test_adjustment_tracking(self):
        """Top-down adjustments should be tracked when blend changes the forecast."""
        parent_forecast = [100.0]
        child_forecasts = {"c5_cracked": [200.0]}  # way above implied 120

        result = allocate_top_down("naphtha", parent_forecast, child_forecasts, blend_ratio=0.5)
        assert len(result.adjustments_made) > 0
        adj = result.adjustments_made[0]
        assert adj["type"] == "top_down_adjustment"
        assert adj["pct_change"] != 0


# ---------------------------------------------------------------------------
# Middle-out constraint
# ---------------------------------------------------------------------------

class TestMiddleOutConstraint:
    """Tests for constrain_middle_out()."""

    def test_no_parent_no_violations(self):
        """Without parent forecast, only sibling ordering is checked."""
        sibling_forecasts = {
            "c5_cracked": [120.0, 125.0],
            "isoprene": [150.0, 155.0],
        }
        result = constrain_middle_out("c5_derivatives", sibling_forecasts)
        assert isinstance(result, MiddleOutConstraintResult)
        # No excessive premium or sibling inversion
        assert all(v["type"] != "excessive_premium" for v in result.violations)

    def test_excessive_premium_detected(self):
        """If sibling > 2x parent, flag excessive premium."""
        sibling_forecasts = {
            "isoprene": [250.0],  # 2.5x parent
        }
        parent_forecast = [100.0]

        result = constrain_middle_out(
            "c5_derivatives", sibling_forecasts,
            parent_forecast=parent_forecast,
        )
        assert any(v["type"] == "excessive_premium" for v in result.violations)

    def test_reasonable_premium_no_violation(self):
        """Reasonable premium (1.5x parent) should not flag."""
        sibling_forecasts = {
            "c5_cracked": [120.0],  # 1.2x parent — reasonable
        }
        parent_forecast = [100.0]

        result = constrain_middle_out(
            "c5_derivatives", sibling_forecasts,
            parent_forecast=parent_forecast,
        )
        assert not any(v["type"] == "excessive_premium" for v in result.violations)

    def test_sibling_inversion_detected(self):
        """If one sibling is >3x another, flag inversion."""
        sibling_forecasts = {
            "isoprene": [300.0, 310.0],
            "c5_cracked": [80.0, 85.0],  # isoprene/c5 ≈ 3.75
        }

        result = constrain_middle_out("c5_derivatives", sibling_forecasts)
        assert any(v["type"] == "sibling_inversion" for v in result.violations)

    def test_no_inversion_when_reasonable_ratio(self):
        """When sibling ratio is reasonable, no inversion flag."""
        sibling_forecasts = {
            "c5_cracked": [120.0, 125.0],
            "isoprene": [150.0, 155.0],  # ratio ≈ 0.8 (isoprene/c5)
        }

        result = constrain_middle_out("c5_derivatives", sibling_forecasts)
        assert not any(v["type"] == "sibling_inversion" for v in result.violations)

    def test_adjusted_forecasts_unchanged_when_no_clamp(self):
        """If no clamping needed, adjusted == original."""
        sibling_forecasts = {
            "c5_cracked": [120.0, 125.0],
            "isoprene": [150.0, 155.0],
        }
        result = constrain_middle_out("c5_derivatives", sibling_forecasts)
        for key in sibling_forecasts:
            assert result.adjusted_forecasts[key] == sibling_forecasts[key]
