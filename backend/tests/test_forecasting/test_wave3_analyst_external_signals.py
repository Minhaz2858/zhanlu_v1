"""Tests for Wave 3 T3.5 — analyst service + evidence_pack + template wiring.

Wires 3 external-feed signals into:
- evidence_pack.build_pack() as optional kwargs
- analyst/service.py _build_pack_for() behind FORECAST_EXTERNAL_SIGNAL_ENABLED flag
- template_brief.py rendering of downstream_utilization / inventory_pressure /
  import_pressure in 供需研判 / 风险提示 sections.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.forecasting.analyst.evidence_pack import build_pack


class TestBuildPackExternalSignals:
    """build_pack() accepts and exposes the 3 Wave 3 external-signal kwargs."""

    def test_default_no_external_keys(self):
        """No new kwargs → evidence pack has empty external-signal keys."""
        pack = build_pack(
            product_id="isoprene", name_zh="异戊二烯", day=7,
            history_rows=[], upstream_histories={},
            run_results={}, model_detail={}, explanation={},
            as_of_month=8,
        )
        # New keys should exist with empty values (zero-regression by default)
        assert "downstream_utilization" in pack
        assert "inventory_pressure" in pack
        assert "import_pressure" in pack
        assert pack["downstream_utilization"] in ({}, None)
        assert pack["inventory_pressure"] in ({}, None)
        assert pack["import_pressure"] in ({}, None)

    def test_passes_through_downstream_utilization(self):
        sig = {"rolling_4wk_op_rate": 75.5, "utilization_regime": "tight",
               "has_sufficient_data": True}
        pack = build_pack(
            product_id="isoprene", name_zh="异戊二烯", day=7,
            history_rows=[], upstream_histories={},
            run_results={}, model_detail={}, explanation={},
            as_of_month=8,
            downstream_utilization=sig,
        )
        assert pack["downstream_utilization"] == sig

    def test_passes_through_inventory_pressure(self):
        sig = {"inventory_4wk_change_pct": 20.0, "inventory_pressure": "high",
               "has_sufficient_data": True}
        pack = build_pack(
            product_id="isoprene", name_zh="异戊二烯", day=7,
            history_rows=[], upstream_histories={},
            run_results={}, model_detail={}, explanation={},
            as_of_month=8,
            inventory_pressure=sig,
        )
        assert pack["inventory_pressure"] == sig

    def test_passes_through_import_pressure(self):
        sig = {"import_parity_gap": 0.20, "ceiling_pressure": True,
               "import_window_open": True, "has_sufficient_data": True}
        pack = build_pack(
            product_id="isoprene", name_zh="异戊二烯", day=7,
            history_rows=[], upstream_histories={},
            run_results={}, model_detail={}, explanation={},
            as_of_month=8,
            import_pressure=sig,
        )
        assert pack["import_pressure"] == sig

    def test_all_three_together(self):
        pack = build_pack(
            product_id="isoprene", name_zh="异戊二烯", day=7,
            history_rows=[], upstream_histories={},
            run_results={}, model_detail={}, explanation={},
            as_of_month=8,
            downstream_utilization={"rolling_4wk_op_rate": 75.0,
                                    "utilization_regime": "tight"},
            inventory_pressure={"inventory_pressure": "high"},
            import_pressure={"ceiling_pressure": True},
        )
        assert pack["downstream_utilization"]["utilization_regime"] == "tight"
        assert pack["inventory_pressure"]["inventory_pressure"] == "high"
        assert pack["import_pressure"]["ceiling_pressure"] is True


class TestAnalystServiceExternalSignals:
    """service.py _build_pack_for() loads external signals behind flag."""

    def _stub_run(self):
        return SimpleNamespace(
            id="r-1",
            results={"best_model": "ets"},
            model_detail={},
            explanation={},
        )

    def test_flag_off_returns_no_external_signals(self, monkeypatch):
        monkeypatch.setenv("FORECAST_EXTERNAL_SIGNAL_ENABLED", "false")
        # Stub out the heavy deps (mds.read_product_history_rows etc.)
        from app.services.forecasting.analyst import service
        monkeypatch.setattr(service, "mds", SimpleNamespace(
            PRODUCT_FORECAST_TARGET_KEY={"isoprene": "isoprene"},
            read_product_history_rows=lambda _p: [],
        ))

        pack = service._build_pack_for("isoprene", 7, self._stub_run())
        assert pack["downstream_utilization"] in ({}, None)
        assert pack["inventory_pressure"] in ({}, None)
        assert pack["import_pressure"] in ({}, None)

    def test_flag_on_loads_external_signals(self, monkeypatch):
        monkeypatch.setenv("FORECAST_EXTERNAL_SIGNAL_ENABLED", "true")
        # Stub mds
        from app.services.forecasting.analyst import service
        monkeypatch.setattr(service, "mds", SimpleNamespace(
            PRODUCT_FORECAST_TARGET_KEY={"isoprene": "isoprene"},
            read_product_history_rows=lambda _p: [],
        ))

        # Stub the 3 loaders with 30+ rows so signal modules have enough data
        from datetime import timedelta
        base = datetime(2025, 1, 1)
        dates = [base + timedelta(days=i) for i in range(60)]
        op_df = pd.DataFrame({"date": dates, "op_rate": [75.0] * 60})
        inv_df = pd.DataFrame({"date": dates, "inventory_t": [5000.0] * 60})
        ip_df = pd.DataFrame({"date": dates, "import_price_cny": [12.0] * 60})

        fake_loaders = SimpleNamespace(
            OperatingRateLoader=lambda **_kw: SimpleNamespace(load=lambda: op_df),
            InventoryLoader=lambda **_kw: SimpleNamespace(load=lambda: inv_df),
            ImportPriceLoader=lambda **_kw: SimpleNamespace(load=lambda: ip_df),
        )

        from app.services.forecasting.features import (
            operating_signal, inventory_signal, import_parity_signal,
        )
        monkeypatch.setattr(service, "compute_operating_signal",
                            operating_signal.compute_operating_signal)
        monkeypatch.setattr(service, "compute_inventory_signal",
                            inventory_signal.compute_inventory_signal)
        monkeypatch.setattr(service, "compute_import_parity_signal",
                            import_parity_signal.compute_import_parity_signal)
        monkeypatch.setattr(service, "OperatingRateLoader",
                            fake_loaders.OperatingRateLoader)
        monkeypatch.setattr(service, "InventoryLoader",
                            fake_loaders.InventoryLoader)
        monkeypatch.setattr(service, "ImportPriceLoader",
                            fake_loaders.ImportPriceLoader)

        pack = service._build_pack_for("isoprene", 7, self._stub_run())
        # Some external-signal data should now be present
        assert pack["downstream_utilization"] != {}
        assert pack["inventory_pressure"] != {}
        assert pack["import_pressure"] != {}


class TestTemplateBriefExternalRender:
    """template_brief.py renders the 3 new fields in 供需研判 / 风险提示."""

    def _build_pack(self, **overrides):
        pack = {
            "product_id": "isoprene",
            "name_zh": "异戊二烯",
            "as_of": "2025-08-05",
            "history_rows": [],
            "run_results": {},
            "models": {"agreement": None},
            "upstream": [],
            "explanation": {"thresholds": {"buy_p": 0.7, "sell_p": 0.3,
                                            "min_change": 0.03,
                                            "edge_accuracy": 0.55}},
            "implied_move_pct": None,
            "divergent": False,
            "seasonal_adj_pct": None,
            "demand": {},
            "supplier_ladder": {},
            "downstream_utilization": {},
            "inventory_pressure": {},
            "import_pressure": {},
        }
        pack.update(overrides)
        return pack

    def test_render_includes_downstream_utilization(self):
        from app.services.forecasting.analyst.template_brief import (
            render_template_brief,
        )
        pack = self._build_pack(downstream_utilization={
            "rolling_4wk_op_rate": 75.5,
            "utilization_regime": "tight",
            "has_sufficient_data": True,
        })
        brief = render_template_brief(pack)
        # Brief is a dict; check supply_demand_zh contains op-rate mention
        assert "开工" in brief["supply_demand_zh"]

    def test_render_includes_inventory_pressure(self):
        from app.services.forecasting.analyst.template_brief import (
            render_template_brief,
        )
        pack = self._build_pack(inventory_pressure={
            "inventory_4wk_change_pct": 20.0,
            "inventory_pressure": "high",
            "has_sufficient_data": True,
        })
        brief = render_template_brief(pack)
        assert "库存" in brief["supply_demand_zh"]

    def test_render_includes_import_pressure_when_window_open(self):
        from app.services.forecasting.analyst.template_brief import (
            render_template_brief,
        )
        pack = self._build_pack(import_pressure={
            "import_parity_gap": 0.20,
            "ceiling_pressure": True,
            "import_window_open": True,
            "has_sufficient_data": True,
        })
        brief = render_template_brief(pack)
        assert "进口" in brief["risk_zh"]

    def test_render_no_external_signals_no_extras(self):
        """Without external signals, brief should still render (graceful)."""
        from app.services.forecasting.analyst.template_brief import (
            render_template_brief,
        )
        pack = self._build_pack()
        brief = render_template_brief(pack)
        # Should not crash; should still produce a dict with all sections
        assert isinstance(brief, dict)
        for key in ("market_update_zh", "supply_demand_zh", "risk_zh"):
            assert key in brief