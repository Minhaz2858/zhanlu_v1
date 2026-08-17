"""Tests for services/alerts/preferences.py — AlertPrefSnapshot + safe_get."""

from types import SimpleNamespace

import pytest

from app.services.alerts.preferences import (
    SEVERITY_ORDER,
    AlertPrefSnapshot,
    safe_get,
)


class _Setting:
    """Minimal stand-in for a UserSetting row (attribute access only)."""

    def __init__(self, **kw):
        self.alert_min_severity = kw.get("alert_min_severity")
        self.alert_muted_products = kw.get("alert_muted_products")
        self.alert_channels = kw.get("alert_channels")
        self.alert_threshold_multiplier = kw.get("alert_threshold_multiplier")
        self.alert_quiet_hours = kw.get("alert_quiet_hours")
        self.alert_recommendations_enabled = kw.get("alert_recommendations_enabled")
        self.alert_webhook_secret = kw.get("alert_webhook_secret")
        self.webhook_url = kw.get("webhook_url")


def _user(uid="user-1"):
    return SimpleNamespace(id=uid, email="a@b.c", full_name="Tester")


# ── safe_get ──────────────────────────────────────────────────────────


def test_safe_get_returns_default_on_missing_key():
    assert safe_get(None, "alert_muted_products", default=[]) == []
    assert safe_get(_Setting(), "alert_muted_products", default=[]) == []


def test_safe_get_tolerates_malformed_json():
    """A JSONB column holding a non-list (e.g. corrupted data) must not crash."""
    setting = _Setting(alert_muted_products="not-a-list")
    assert safe_get(setting, "alert_muted_products", default=[]) == []


def test_safe_get_returns_value_when_present():
    setting = _Setting(alert_min_severity="high")
    assert safe_get(setting, "alert_min_severity", default="low") == "high"


# ── from_user ─────────────────────────────────────────────────────────


def test_from_user_applies_defaults_when_no_setting():
    snap = AlertPrefSnapshot.from_user(_user(), None)
    assert snap.user_id == "user-1"
    assert snap.min_severity == "low"
    assert snap.muted_products == []
    assert snap.channels == ["in_app"]
    assert snap.threshold_multiplier == 1.0
    assert snap.quiet_hours is None
    assert snap.recommendations_enabled is True
    assert snap.webhook_url is None
    assert snap.webhook_secret is None


def test_from_user_maps_all_columns():
    setting = _Setting(
        alert_min_severity="high",
        alert_muted_products=["p1", "p2"],
        alert_channels=["in_app", "webhook"],
        alert_threshold_multiplier=1.5,
        alert_quiet_hours={"enabled": True, "start": "22:00", "end": "07:00"},
        alert_recommendations_enabled=False,
        alert_webhook_secret="sec",
        webhook_url="https://hooks.example.com/x",
    )
    snap = AlertPrefSnapshot.from_user(_user(), setting)
    assert snap.min_severity == "high"
    assert snap.muted_products == ["p1", "p2"]
    assert snap.channels == ["in_app", "webhook"]
    assert snap.threshold_multiplier == 1.5
    assert snap.quiet_hours["enabled"] is True
    assert snap.recommendations_enabled is False
    assert snap.webhook_secret == "sec"
    assert snap.webhook_url == "https://hooks.example.com/x"


def test_from_user_tolerates_malformed_muted_products():
    setting = _Setting(alert_muted_products="corrupted-json")
    snap = AlertPrefSnapshot.from_user(_user(), setting)
    assert snap.muted_products == []


# ── decision helpers ──────────────────────────────────────────────────


def test_is_muted_filters_product():
    snap = AlertPrefSnapshot(user_id="u1", muted_products=["p1", "p2"])
    assert snap.is_muted("p1") is True
    assert snap.is_muted("p3") is False
    assert AlertPrefSnapshot(user_id="u1").is_muted("p1") is False


def test_meets_min_severity_floor():
    snap = AlertPrefSnapshot(user_id="u1", min_severity="high")
    assert snap.meets_min_severity("high") is True
    assert snap.meets_min_severity("medium") is False
    assert snap.meets_min_severity("low") is False
    # default floor is "low" → everything passes
    default = AlertPrefSnapshot(user_id="u1")
    assert default.meets_min_severity("low") is True
    assert default.meets_min_severity("high") is True


def test_severity_order_mapping():
    assert SEVERITY_ORDER == {"low": 0, "medium": 1, "high": 2}


def test_multiplier_scale_is_preserved():
    snap = AlertPrefSnapshot(user_id="u1", threshold_multiplier=2.0)
    assert snap.threshold_multiplier == 2.0
