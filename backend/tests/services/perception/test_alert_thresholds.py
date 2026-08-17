"""Regression tests: alert detection thresholds are read from Settings.

The bug (fixed): detection used hardcoded ±3% / ±6% constants baked into
``AlertService.detect_and_persist``, so tuning them required a code edit.
Now they flow from ``settings.ALERT_PRICE_SPIKE_THRESHOLD`` /
``ALERT_PRICE_DROP_THRESHOLD`` / ``ALERT_HIGH_SEVERITY_THRESHOLD`` via
``effective_thresholds()``, with the module constants kept as fallback defaults.

These tests pin:
* ``effective_thresholds()`` reflects the configured settings values,
* ``detect_and_persist`` honors a lowered spike threshold (e.g. 1.5%)
  instead of the hardcoded 3% default.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.perception.alert_service import (
    HIGH_SEVERITY_THRESHOLD,
    PRICE_DROP_THRESHOLD,
    PRICE_SPIKE_THRESHOLD,
    AlertService,
    effective_thresholds,
)


class _FakeQuery:
    def __init__(self):
        self._row = None

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._row


class _FakeDB:
    """Minimal stand-in for the SQLAlchemy session used by detect_and_persist."""

    def __init__(self, existing=None):
        self.added: list = []
        self._query = _FakeQuery()
        self._query._row = existing

    def query(self, _model):
        return self._query

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass


def test_module_constants_match_defaults():
    # Defaults preserved so a config-less deployment behaves identically.
    assert PRICE_SPIKE_THRESHOLD == 3.0
    assert PRICE_DROP_THRESHOLD == -3.0
    assert HIGH_SEVERITY_THRESHOLD == 6.0


def test_effective_thresholds_returns_configured_settings(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ALERT_PRICE_SPIKE_THRESHOLD", 1.5)
    monkeypatch.setattr(settings, "ALERT_PRICE_DROP_THRESHOLD", -1.5)
    monkeypatch.setattr(settings, "ALERT_HIGH_SEVERITY_THRESHOLD", 4.0)

    spike, drop, high = effective_thresholds()
    assert spike == 1.5
    assert drop == -1.5
    assert high == 4.0


def test_effective_thresholds_falls_back_to_constants(monkeypatch):
    from app.config import settings

    for name in ("ALERT_PRICE_SPIKE_THRESHOLD", "ALERT_PRICE_DROP_THRESHOLD",
                 "ALERT_HIGH_SEVERITY_THRESHOLD"):
        monkeypatch.delattr(settings, name, raising=False)

    spike, drop, high = effective_thresholds()
    assert spike == PRICE_SPIKE_THRESHOLD
    assert drop == PRICE_DROP_THRESHOLD
    assert high == HIGH_SEVERITY_THRESHOLD


def test_detect_and_persist_honors_lowered_spike_threshold(monkeypatch):
    """A +2% change is below the 3% default but must fire with spike=1.5%."""
    from app.config import settings

    monkeypatch.setattr(settings, "ALERT_PRICE_SPIKE_THRESHOLD", 1.5)
    monkeypatch.setattr(settings, "ALERT_PRICE_DROP_THRESHOLD", -1.5)
    monkeypatch.setattr(settings, "ALERT_HIGH_SEVERITY_THRESHOLD", 6.0)

    db = _FakeDB(existing=None)  # no dedup hit → new alert persisted

    with patch(
        "app.services.perception.alert_service.AlertService.recent_alert_exists",
        return_value=False,
    ):
        new_alerts = AlertService.detect_and_persist(
            db,
            "org-1",
            [{
                "product_id": "cracked_c5",
                "product_name": "裂解C5",
                "current_price": 102.0,
                "change_7d_pct": 2.0,
            }],
        )

    assert len(new_alerts) == 1
    assert new_alerts[0]["alert_type"] == "price_spike"
    assert new_alerts[0]["severity"] == "medium"


def test_detect_and_persist_skips_below_default_threshold(monkeypatch):
    """Without tuning, +2% stays under the 3% spike threshold → no alert."""
    from app.config import settings

    # Explicitly restore defaults in case another test mutated settings.
    monkeypatch.setattr(settings, "ALERT_PRICE_SPIKE_THRESHOLD", 3.0)
    monkeypatch.setattr(settings, "ALERT_PRICE_DROP_THRESHOLD", -3.0)
    monkeypatch.setattr(settings, "ALERT_HIGH_SEVERITY_THRESHOLD", 6.0)

    db = _FakeDB(existing=None)

    with patch(
        "app.services.perception.alert_service.AlertService.recent_alert_exists",
        return_value=False,
    ):
        new_alerts = AlertService.detect_and_persist(
            db,
            "org-1",
            [{
                "product_id": "cracked_c5",
                "product_name": "裂解C5",
                "current_price": 102.0,
                "change_7d_pct": 2.0,
            }],
        )

    assert new_alerts == []
