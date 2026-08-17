"""Regression tests for the alert pipeline diagnostics report.

Pins the shape of ``build_alert_diagnostics()`` (the data behind
``GET /api/alerts/diagnostics``) so the frontend banner contract can't drift:
flags, warehouse reachability, thresholds, effective_org_id, per-product probe,
and triggering_products must all be present and consistent with each other.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.perception.diagnostics import build_alert_diagnostics


def _fake_graph(nodes: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(nodes=nodes)


def _node(pid: str, name_en: str = "", name_cn: str = "") -> SimpleNamespace:
    return SimpleNamespace(id=pid, name_en=name_en or pid, name_cn=name_cn or pid)


def _patch_probe(price_info: dict | None = None, rows: list | None = None):
    """Patch all external dependencies of build_alert_diagnostics."""
    return patch.multiple(
        "app.services.perception.diagnostics",
        is_edia_reachable=lambda: True,
        read_product_history_rows=lambda _pid: rows or [],
        PerceptionService=SimpleNamespace(
            _get_product_price=staticmethod(lambda _pid: price_info or {})
        ),
    )


def test_diagnostics_reports_flags_warehouse_and_org(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "EDIA_MYSQL_URL", "mysql://warehouse")
    monkeypatch.setattr(settings, "ECISCO_BI_ALERTS_ENABLED", True)
    monkeypatch.setattr(settings, "ALERT_DIGEST_ENABLED", False)
    monkeypatch.setattr(settings, "DEFAULT_ORG_ID", "org-canonical")

    with patch(
        "app.services.perception.diagnostics.build_c5_c9_graph",
        return_value=_fake_graph([_node("cracked_c5", "Cracked C5", "裂解C5")]),
    ), _patch_probe(price_info=None, rows=[]):
        report = build_alert_diagnostics(db=None)

    assert report["flags"] == {
        "ecisco_bi_alerts_enabled": True,
        "alert_digest_enabled": False,
    }
    assert report["warehouse"] == {"configured": True, "reachable": True}
    assert report["effective_org_id"] == "org-canonical"
    assert len(report["products"]) == 1
    assert report["products"][0]["product_id"] == "cracked_c5"
    assert report["products"][0]["row_count"] == 0
    assert report["products"][0]["latest_price"] is None
    assert report["triggering_products"] == []


def test_diagnostics_flags_unreachable_warehouse(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "EDIA_MYSQL_URL", "mysql://warehouse")

    with patch(
        "app.services.perception.diagnostics.build_c5_c9_graph",
        return_value=_fake_graph([_node("cracked_c5")]),
    ), patch(
        "app.services.perception.diagnostics.is_edia_reachable", return_value=False
    ), patch(
        "app.services.perception.diagnostics.read_product_history_rows",
        return_value=[],
    ):
        report = build_alert_diagnostics(db=None)

    assert report["warehouse"] == {"configured": True, "reachable": False}


def test_diagnostics_marks_unconfigured_warehouse(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "EDIA_MYSQL_URL", "")

    with patch(
        "app.services.perception.diagnostics.build_c5_c9_graph",
        return_value=_fake_graph([]),
    ):
        report = build_alert_diagnostics(db=None)

    assert report["warehouse"] == {"configured": False, "reachable": False}
    assert report["products"] == []


def test_diagnostics_classifies_triggering_products(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "EDIA_MYSQL_URL", "mysql://warehouse")
    monkeypatch.setattr(settings, "ALERT_PRICE_SPIKE_THRESHOLD", 3.0)
    monkeypatch.setattr(settings, "ALERT_PRICE_DROP_THRESHOLD", -3.0)
    monkeypatch.setattr(settings, "ALERT_HIGH_SEVERITY_THRESHOLD", 6.0)

    graph = _fake_graph([
        _node("cracked_c5", "Cracked C5", "裂解C5"),
        _node("trimethylbenzene", "TMB", "均三甲苯"),
        _node("stable_product", "Stable", "平稳品"),
    ])

    price_infos = {
        "cracked_c5": {"latest_price": 110.0, "latest_date": "2026-08-17",
                       "change_7d_pct": 5.0},   # spike (medium)
        "trimethylbenzene": {"latest_price": 90.0, "latest_date": "2026-08-17",
                             "change_7d_pct": -7.0},  # drop (high)
        "stable_product": {"latest_price": 100.0, "latest_date": "2026-08-17",
                           "change_7d_pct": 1.0},     # within range
    }

    def fake_price(pid):
        return price_infos.get(pid, {})

    with patch(
        "app.services.perception.diagnostics.build_c5_c9_graph", return_value=graph
    ), patch(
        "app.services.perception.diagnostics.is_edia_reachable", return_value=True
    ), patch(
        "app.services.perception.diagnostics.read_product_history_rows",
        return_value=[("2026-08-10", 100.0), ("2026-08-17", 105.0)],
    ), patch(
        "app.services.perception.diagnostics.PerceptionService",
        SimpleNamespace(_get_product_price=staticmethod(fake_price)),
    ):
        report = build_alert_diagnostics(db=None)

    by_id = {p["product_id"]: p for p in report["products"]}

    assert by_id["cracked_c5"]["would_trigger"] is True
    assert by_id["cracked_c5"]["trigger_type"] == "price_spike/medium"

    assert by_id["trimethylbenzene"]["would_trigger"] is True
    assert by_id["trimethylbenzene"]["trigger_type"] == "price_drop/high"

    assert by_id["stable_product"]["would_trigger"] is False
    assert by_id["stable_product"]["trigger_type"] is None

    assert set(report["triggering_products"]) == {"cracked_c5", "trimethylbenzene"}

    # Thresholds section reflects live settings.
    assert report["thresholds"] == {
        "price_spike": 3.0,
        "price_drop": -3.0,
        "high_severity": 6.0,
    }
