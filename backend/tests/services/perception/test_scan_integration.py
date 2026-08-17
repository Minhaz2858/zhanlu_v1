"""Integration tests for the alert scan cycle (worker → evaluator → persist).

Covers:
  - No-regression: with SMART_ALERTS_ENABLED=False the static threshold path
    is byte-identical (uses effective_thresholds(), no _smart payloads).
  - Smart path: SMART_ALERTS_ENABLED=True routes products through
    SmartEvaluator; persisted AlertLog carries impact + ctx payload.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.perception.alert_service import AlertService
from app.services.perception.smart_evaluator import SmartEvaluator


class FakeDB:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.added: list = []
        self.committed = 0

    def add(self, obj):
        self.added.append(obj)
        self.rows[obj.product_id] = obj

    def commit(self):
        self.committed += 1


_SNAPSHOT = [
    {
        "product_id": "c5_resin",
        "product_name": "C5石油树脂",
        "current_price": 9900.0,
        "change_7d_pct": 16.82,
    },
    {
        "product_id": "stable_prod",
        "product_name": "稳定产品",
        "current_price": 1000.0,
        "change_7d_pct": 0.5,  # below static +3% and adaptive thresholds
    },
]


def test_static_path_unchanged_when_flag_off():
    """SMART_ALERTS_ENABLED=False → static thresholds, no smart payload."""
    db = FakeDB()
    with (
        patch("app.config.settings.SMART_ALERTS_ENABLED", False),
        patch.object(AlertService, "recent_alert_exists", return_value=False),
        patch("app.config.settings.ALERT_PRICE_SPIKE_THRESHOLD", 3.0),
        patch("app.config.settings.ALERT_PRICE_DROP_THRESHOLD", -3.0),
    ):
        alerts = AlertService.detect_and_persist(db, "org-1", _SNAPSHOT)

    # Only c5_resin (16.82% >= +3%) triggers; stable_prod is skipped.
    assert [a["product_id"] for a in alerts] == ["c5_resin"]
    assert alerts[0]["severity"] == "high"
    assert alerts[0]["payload"] is None  # static mode never writes payload


def test_smart_path_routes_through_evaluator_and_populates_payload():
    """SMART_ALERTS_ENABLED=True → evaluator filters + payload persisted."""
    from app.services.perception.baseline_cache import BaselineCache

    class EvalDB(FakeDB):
        def get(self, _model, pk):
            return self.rows.get(pk)

    db = EvalDB()
    # Stable product has a high baseline (volatile) so it does NOT trigger;
    # c5_resin has a low baseline so 16.82% triggers.
    from datetime import datetime, timezone

    from app.models.alert_baseline import AlertProductBaseline

    db.rows["stable_prod"] = AlertProductBaseline(
        product_id="stable_prod",
        baseline_pct=10.0,
        computed_at=datetime.now(timezone.utc),
        model_used="test",
    )
    db.rows["c5_resin"] = AlertProductBaseline(
        product_id="c5_resin",
        baseline_pct=2.0,
        computed_at=datetime.now(timezone.utc),
        model_used="test",
    )

    class Pref:
        muted_products = []
        min_severity = "low"
        threshold_multiplier = 1.0
        recommendations_enabled = True

    with (
        patch("app.config.settings.SMART_ALERTS_ENABLED", True),
        patch("app.config.settings.ALERT_IMPACT_ENABLED", True),
        patch.object(AlertService, "recent_alert_exists", return_value=False),
        patch("app.services.perception.smart_evaluator.compute_impact",
              return_value={"impact_yuan_per_day": 999.0, "avg_daily_volume_tons": 12.0}),
    ):
        evaluator = SmartEvaluator(db=db)
        enriched = evaluator.evaluate(_SNAPSHOT, Pref())
        alerts = AlertService.detect_and_persist(db, "org-1", enriched)

    # Only c5_resin survives the evaluator (stable_prod baseline 10% > 0.5%).
    assert [a["product_id"] for a in alerts] == ["c5_resin"]
    assert alerts[0]["payload"]["smart"] is True
    assert alerts[0]["payload"]["impact"] is not None
    assert alerts[0]["payload"]["ctx"] is not None
    assert alerts[0]["payload"]["baseline_used"] == 2.0
