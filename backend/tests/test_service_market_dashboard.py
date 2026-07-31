"""market_dashboard_service — snapshot/history read paths + unmapped fallback."""
import pytest
from fastapi import HTTPException

from app.services import market_dashboard_service as svc


def test_is_edia_enabled_when_url_set(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x",
    )
    assert svc.is_edia_enabled() is True


def test_get_market_snapshot_unconfigured_returns_503(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL", ""
    )
    with pytest.raises(HTTPException) as exc:
        svc.get_market_snapshot(group=None)
    assert exc.value.status_code == 503
    assert exc.value.detail == "edia_unavailable"


def test_get_market_snapshot_for_unmapped_product_returns_note(monkeypatch):
    """Products without a table mapping surface as null price + note."""
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x",
    )
    monkeypatch.setattr(svc, "is_edia_reachable_cached", lambda: True)
    monkeypatch.setattr(svc, "_read_product_history_rows", lambda pid: [])
    snap = svc.get_market_snapshot(group="upstream")
    crude = next(s for s in snap if s.id == "crude_oil")
    assert crude.price is None
    assert "not configured" in (crude.note or "")


def test_get_market_snapshot_mapped_product_uses_history(monkeypatch):
    """History: two points; last = 612.5, prev = 600.0 → change = +2.083%."""
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x",
    )
    monkeypatch.setattr(svc, "is_edia_reachable_cached", lambda: True)

    def mock_history(pid):
        if pid == "naphtha":
            return [("2026-07-29", 600.0), ("2026-07-30", 612.5)]
        return []

    monkeypatch.setattr(svc, "_read_product_history_rows", mock_history)
    snap = svc.get_market_snapshot(group="upstream")
    naphtha = next(s for s in snap if s.id == "naphtha")
    assert naphtha.price == 612.5
    assert naphtha.date == "2026-07-30"
    assert abs(naphtha.change_pct - 2.08) < 0.05


def test_get_price_history_unconfigured_returns_503(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL", ""
    )
    with pytest.raises(HTTPException) as exc:
        svc.get_price_history(product_id="naphtha", days=30)
    assert exc.value.detail == "edia_unavailable"


def test_get_price_history_unmapped_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x",
    )
    monkeypatch.setattr(svc, "is_edia_reachable_cached", lambda: True)
    monkeypatch.setattr(svc, "_read_product_history_rows", lambda pid: [])
    assert svc.get_price_history("crude_oil", days=30) == []
