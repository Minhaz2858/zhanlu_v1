"""Tests for Wave 1: _supply_demand() renders demand/supplier text."""
from __future__ import annotations

import pytest

from app.services.forecasting.analyst.template_brief import _supply_demand


class TestSupplyDemandRendersDemand:
    """_supply_demand() includes demand signal when 'demand' key present."""

    def test_renders_up_trend(self):
        pack = {
            "demand": {
                "has_sufficient_data": True,
                "rolling_4wk_vol": 1234.5,
                "demand_trend": "up",
            },
            "supplier_ladder": {},
        }
        result = _supply_demand(pack)
        assert "需求上行" in result
        assert "1234" in result

    def test_renders_down_trend(self):
        pack = {
            "demand": {
                "has_sufficient_data": True,
                "rolling_4wk_vol": 500.0,
                "demand_trend": "down",
            },
            "supplier_ladder": {},
        }
        result = _supply_demand(pack)
        assert "需求收缩" in result

    def test_renders_flat_trend(self):
        pack = {
            "demand": {
                "has_sufficient_data": True,
                "rolling_4wk_vol": 800.0,
                "demand_trend": "flat",
            },
            "supplier_ladder": {},
        }
        result = _supply_demand(pack)
        assert "需求平稳" in result

    def test_renders_yoy_positive(self):
        pack = {
            "demand": {
                "has_sufficient_data": True,
                "rolling_4wk_vol": 1000,
                "demand_trend": "up",
                "yoy_change_pct": 0.125,
            },
            "supplier_ladder": {},
        }
        result = _supply_demand(pack)
        assert "同比+12%" in result

    def test_renders_yoy_negative(self):
        pack = {
            "demand": {
                "has_sufficient_data": True,
                "rolling_4wk_vol": 1000,
                "demand_trend": "down",
                "yoy_change_pct": -0.15,
            },
            "supplier_ladder": {},
        }
        result = _supply_demand(pack)
        assert "同比-15%" in result

    def test_suppresses_tiny_yoy(self):
        """YoY < 1% should not appear in the text."""
        pack = {
            "demand": {
                "has_sufficient_data": True,
                "rolling_4wk_vol": 1000,
                "demand_trend": "flat",
                "yoy_change_pct": 0.005,
            },
            "supplier_ladder": {},
        }
        result = _supply_demand(pack)
        assert "同比" not in result

    def test_renders_divergence(self):
        pack = {
            "demand": {
                "has_sufficient_data": True,
                "rolling_4wk_vol": 1000,
                "demand_trend": "up",
                "vol_price_divergence": 0.35,
            },
            "supplier_ladder": {},
        }
        result = _supply_demand(pack)
        assert "量价背离" in result

    def test_renders_convergence(self):
        pack = {
            "demand": {
                "has_sufficient_data": True,
                "rolling_4wk_vol": 1000,
                "demand_trend": "up",
                "vol_price_divergence": -0.25,
            },
            "supplier_ladder": {},
        }
        result = _supply_demand(pack)
        assert "量价收敛" in result

    def test_suppresses_tiny_divergence(self):
        """Divergence < 0.1 should not appear."""
        pack = {
            "demand": {
                "has_sufficient_data": True,
                "rolling_4wk_vol": 1000,
                "demand_trend": "up",
                "vol_price_divergence": 0.05,
            },
            "supplier_ladder": {},
        }
        result = _supply_demand(pack)
        assert "量价" not in result


class TestSupplyDemandNoDemandUnchanged:
    """Without demand/supplier data, existing seasonal/drivers/agreement still rendered."""

    def test_seasonal_still_renders(self):
        pack = {
            "seasonal": {"month": 1, "label_zh": "传统需求旺季", "adj_pct": 5.0},
        }
        result = _supply_demand(pack)
        assert "传统需求旺季" in result
        assert "季节调整" in result

    def test_empty_demand_no_effect(self):
        pack = {
            "demand": {},
            "supplier_ladder": {},
            "seasonal": {"month": 3, "label_zh": "传统需求淡季", "adj_pct": -3.0},
        }
        result = _supply_demand(pack)
        assert "传统需求淡季" in result
        assert "需求上行" not in result
        assert "需求收缩" not in result
        assert "需求平稳" not in result

    def test_demand_insufficient_data_skipped(self):
        """demand has_sufficient_data=False → demand text not rendered."""
        pack = {
            "demand": {
                "has_sufficient_data": False,
                "rolling_4wk_vol": 100,
            },
            "supplier_ladder": {},
            "seasonal": {"month": 6, "label_zh": "传统需求淡季", "adj_pct": -2.0},
        }
        result = _supply_demand(pack)
        assert "需求上行" not in result


class TestSupplyDemandSupplierLadder:
    """_supply_demand() includes supplier ladder signal."""

    def test_renders_supplier(self):
        pack = {
            "supplier_ladder": {
                "has_data": True,
                "avg_spread": 350.0,
                "spread_trend": "走阔",
                "recent_days": 30,
            },
        }
        result = _supply_demand(pack)
        assert "供应商价差" in result
        assert "350" in result
        assert "走阔" in result

    def test_supplier_no_data_skipped(self):
        pack = {
            "supplier_ladder": {"has_data": False, "avg_spread": 100},
            "seasonal": {"month": 1, "label_zh": "旺季", "adj_pct": 5.0},
        }
        result = _supply_demand(pack)
        assert "供应商价差" not in result
        assert "旺季" in result
