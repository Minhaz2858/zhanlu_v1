"""Tests for the shared what-if simulation helper (app.services.forecasting.what_if)."""
from datetime import datetime, timezone

import pytest

from app.models.forecasting import ForecastRun, ForecastTarget
from app.services.forecasting import what_if


class FakeQuery:
    def __init__(self, db, model):
        self._db = db
        self._model = model

    def filter_by(self, **kwargs):
        self._filters = kwargs
        return self

    def filter(self, *args):
        return self

    def order_by(self, *args):
        return self

    def first(self):
        if self._model is ForecastTarget:
            return self._db._target
        return self._db._run


class FakeDB:
    def __init__(self, target=None, run=None):
        self._target = target
        self._run = run

    def query(self, model):
        return FakeQuery(self, model)


def _target(product_key="乙烯"):
    return ForecastTarget(product_key=product_key, name=product_key,
                          org_id="default-org", id=1)


def _run(point_forecast=(100.0, 101.0, 102.0)):
    return ForecastRun(
        target_id=1,
        results={"7d": {"base": list(point_forecast),
                        "bull": [110.0] * len(point_forecast),
                        "bear": [90.0] * len(point_forecast)}},
        as_of_date=datetime.now(timezone.utc),
    )


def test_compute_what_if_returns_full_shape(monkeypatch):
    """dict shape: product_key, base/adjusted forecast, total_impact, adjustments."""
    monkeypatch.setattr(
        what_if, "compute_domain_signal_adjustment",
        lambda **kw: {"causal_pct": 5.0},
    )
    result = what_if.compute_what_if(
        product_key="乙烯",
        market_delta_pct=0.0,
        feedstock_delta_pct=10.0,
        db=FakeDB(target=_target(), run=_run()),
    )
    assert result["product_key"] == "乙烯"
    assert result["base_forecast"] == [100.0, 101.0, 102.0]
    assert result["total_impact_pct"] == 5.0
    # The root-feedstock driver label is config-driven; empty config → generic
    # "feedstock" (was hardcoded "naphtha" in the pre-de-hardcoding era).
    assert result["adjustments"][0]["driver"] == "feedstock"
    assert result["adjustments"][0]["impact_pct"] == 5.0
    assert result["adjusted_forecast"] == [
        round(100.0 * 1.05, 2), round(101.0 * 1.05, 2), round(102.0 * 1.05, 2),
    ]


def test_compute_what_if_raises_lookup_error_when_target_missing():
    """Raises LookupError when no ForecastTarget matches the product_key."""
    with pytest.raises(LookupError, match="no forecast target for 'unknown'"):
        what_if.compute_what_if(
            product_key="unknown",
            market_delta_pct=0.0,
            feedstock_delta_pct=0.0,
            db=FakeDB(target=None),
        )


def test_compute_what_if_market_uses_08_elasticity_proxy(monkeypatch):
    """Market shock is converted to the root feedstock with ~0.8 elasticity before propagation."""
    calls = {}

    def fake_adjustment(product_id, as_of_date, naphtha_pct_change):
        calls["naphtha_pct_change"] = naphtha_pct_change
        return {"causal_pct": 2.0}

    monkeypatch.setattr(what_if, "compute_domain_signal_adjustment", fake_adjustment)
    result = what_if.compute_what_if(
        product_key="乙烯",
        market_delta_pct=5.0,
        feedstock_delta_pct=0.0,
        db=FakeDB(target=_target(), run=_run()),
    )
    assert calls["naphtha_pct_change"] == 5.0 * 0.8  # 4.0
    assert result["adjustments"][0]["driver"] == "market_index"
    assert result["total_impact_pct"] == 2.0
