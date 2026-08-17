"""Tests for SmartEvaluator (services/perception/smart_evaluator.py).

Covers: adaptive trigger rules, severity mapping, muted-product filtering,
user threshold-multiplier interaction, high-severity floor clamp, and the
enriched payload (impact + context + baseline_used).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.alert_baseline import AlertProductBaseline
from app.services.perception.baseline_cache import BaselineCache
from app.services.perception.smart_evaluator import SmartEvaluator


class FakeDB:
    def __init__(self):
        self.rows: dict[str, AlertProductBaseline] = {}

    def get(self, _model, pk):
        return self.rows.get(pk)

    def add(self, obj):
        self.rows[obj.product_id] = obj

    def commit(self):
        pass


class FakePref:
    """Duck-typed stand-in for AlertPrefSnapshot (defined in Task 5)."""

    def __init__(self, muted=None, min_severity="low", multiplier=1.0):
        self.muted_products = list(muted or [])
        self.min_severity = min_severity
        self.threshold_multiplier = multiplier


def _baseline(pid: str, baseline_pct: float) -> AlertProductBaseline:
    return AlertProductBaseline(
        product_id=pid,
        baseline_pct=baseline_pct,
        reasoning="test",
        computed_at=datetime.now(timezone.utc),
        model_used="test",
    )


def _product(pid: str = "c5_resin", name: str = "C5石油树脂",
             price: float = 9900.0, change: float = 16.82) -> dict:
    return {
        "product_id": pid,
        "product_name": name,
        "current_price": price,
        "change_7d_pct": change,
    }


def _evaluator(db: FakeDB) -> SmartEvaluator:
    return SmartEvaluator(db=db)  # BaselineCache(FakeDB) constructed internally


def _impact_dict() -> dict:
    return {"impact_yuan_per_day": 99000.0, "avg_daily_volume_tons": 120.0}


def test_no_trigger_when_change_below_baseline():
    db = FakeDB()
    db.rows["c5_resin"] = _baseline("c5_resin", 5.0)  # volatile product
    ev = _evaluator(db)

    with patch(
        "app.services.perception.smart_evaluator.compute_impact",
        return_value=_impact_dict(),
    ):
        result = ev.evaluate([_product(change=3.0)], FakePref())

    assert result == []


def test_triggers_when_change_exceeds_baseline():
    db = FakeDB()
    db.rows["c5_resin"] = _baseline("c5_resin", 2.0)
    ev = _evaluator(db)

    with patch(
        "app.services.perception.smart_evaluator.compute_impact",
        return_value=_impact_dict(),
    ):
        result = ev.evaluate([_product(change=4.0)], FakePref())

    assert len(result) == 1
    assert result[0]["product_id"] == "c5_resin"
    assert result[0]["baseline_used"] == 2.0


def test_muted_products_are_skipped():
    db = FakeDB()
    db.rows["c5_resin"] = _baseline("c5_resin", 1.0)
    ev = _evaluator(db)

    result = ev.evaluate([_product(change=9.0)], FakePref(muted=["c5_resin"]))

    assert result == []


def test_severity_medium_below_double_baseline():
    """change < max(baseline*2, floor) → medium."""
    db = FakeDB()
    db.rows["c5_resin"] = _baseline("c5_resin", 2.0)  # 2*2=4 < floor 6
    ev = _evaluator(db)

    with patch(
        "app.services.perception.smart_evaluator.compute_impact",
        return_value=_impact_dict(),
    ):
        result = ev.evaluate([_product(change=4.5)], FakePref())

    assert result[0]["severity"] == "medium"


def test_severity_high_when_change_at_floor():
    """change >= max(baseline*2, floor=6.0) → high."""
    db = FakeDB()
    db.rows["c5_resin"] = _baseline("c5_resin", 2.0)
    ev = _evaluator(db)

    with patch(
        "app.services.perception.smart_evaluator.compute_impact",
        return_value=_impact_dict(),
    ):
        result = ev.evaluate([_product(change=7.0)], FakePref())

    assert result[0]["severity"] == "high"


def test_min_severity_filter_filters_medium():
    db = FakeDB()
    db.rows["c5_resin"] = _baseline("c5_resin", 2.0)
    ev = _evaluator(db)

    with patch(
        "app.services.perception.smart_evaluator.compute_impact",
        return_value=_impact_dict(),
    ):
        result = ev.evaluate([_product(change=4.5)], FakePref(min_severity="high"))

    assert result == []


def test_threshold_multiplier_raises_trigger_bar():
    """2.0× multiplier doubles the effective baseline threshold."""
    db = FakeDB()
    db.rows["c5_resin"] = _baseline("c5_resin", 2.0)  # effective threshold 4.0
    ev = _evaluator(db)

    with patch(
        "app.services.perception.smart_evaluator.compute_impact",
        return_value=_impact_dict(),
    ):
        # change 3.9 < 4.0 → no trigger despite exceeding bare baseline
        assert ev.evaluate([_product(change=3.9)], FakePref(multiplier=2.0)) == []
        # change 4.1 >= 4.0 → triggers
        assert len(ev.evaluate([_product(change=4.1)], FakePref(multiplier=2.0))) == 1


def test_threshold_multiplier_lowers_trigger_bar():
    db = FakeDB()
    db.rows["c5_resin"] = _baseline("c5_resin", 2.0)  # effective threshold 1.0
    ev = _evaluator(db)

    with patch(
        "app.services.perception.smart_evaluator.compute_impact",
        return_value=_impact_dict(),
    ):
        # change 1.2 >= 1.0 → triggers even though below bare baseline
        result = ev.evaluate([_product(change=1.2)], FakePref(multiplier=0.5))

    assert len(result) == 1


def test_enriched_payload_includes_impact_ctx_and_baseline():
    db = FakeDB()
    db.rows["c5_resin"] = _baseline("c5_resin", 2.0)
    ev = _evaluator(db)

    with patch(
        "app.services.perception.smart_evaluator.compute_impact",
        return_value=_impact_dict(),
    ):
        result = ev.evaluate([_product(change=7.0)], FakePref())

    entry = result[0]
    assert entry["impact"] == _impact_dict()
    assert entry["ctx"] is not None
    assert entry["ctx"]["why"]
    assert entry["ctx"]["recommendation"]["title"]
    assert entry["ctx"]["recommendation"]["steps"]
    assert entry["baseline_used"] == 2.0


def test_impact_disabled_returns_none():
    db = FakeDB()
    db.rows["c5_resin"] = _baseline("c5_resin", 2.0)
    ev = _evaluator(db)

    with patch("app.config.settings.ALERT_IMPACT_ENABLED", False):
        result = ev.evaluate([_product(change=7.0)], FakePref())

    assert result[0]["impact"] is None
