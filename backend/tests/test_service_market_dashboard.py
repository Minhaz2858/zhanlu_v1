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
    """Unknown product_ids surface as null price + note; known mapped ones work."""
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x",
    )
    monkeypatch.setattr(svc, "is_edia_reachable_cached", lambda: True)

    # Map every product to empty history so snapshot still runs without DB.
    monkeypatch.setattr(svc, "_read_product_history_rows", lambda pid: [])
    snap = svc.get_market_snapshot(group="upstream")

    # cracked_c9 is now mapped (裂解c9 table) → returns null because no rows
    # came back from the (mocked) history, but the note is the "no rows"
    # variant, not the "not configured" variant.
    cracked = next(s for s in snap if s.id == "cracked_c9")
    assert cracked.price is None
    assert "not configured" not in (cracked.note or "")


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


def test_get_regional_prices_unmapped_returns_empty(monkeypatch):
    """Unknown product_ids surface as empty regions + 'not configured' note."""
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x",
    )
    monkeypatch.setattr(svc, "is_edia_reachable_cached", lambda: True)
    # Pass an obviously unknown product_id; the scanner must short-circuit
    # on the missing cfg entry and never touch the DB.
    r = svc.get_regional_prices("__unknown_product__")
    assert r.regions == []
    assert "not configured" in (r.note or "")


def test_get_news_events_v1_returns_empty_with_note(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x",
    )
    monkeypatch.setattr(svc, "is_edia_reachable_cached", lambda: True)
    r = svc.get_news_events(group="upstream", limit=18)
    assert r.items == []
    assert "AI event pipeline" in (r.note or "")


# ── Overview bundle (one-shot endpoint) ───────────────────────────────────

def test_overview_bundle_uses_cache_after_first_call(monkeypatch):
    """The second call within 30s should return cached=True and skip the DB."""
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x",
    )
    # Don't actually call the DB — just confirm the cache plumbing works.
    svc._overview_cache.clear()
    from app.schemas.market_dashboard import FxRateResponse
    sentinel = {
        "snapshot": [],
        "regional": {},
        "regional_errors": {},
        "forecast_day": 7,
        "forecast_base_by_product": {},
        "fx": FxRateResponse(
            price=7.2, unit="CNY/USD", as_of_date="2026-08-01",
            series_product_id="usd_cny", series_price_type="spot",
            data_source="static", available=True,
        ).model_dump(),
        "cached": False,
        "cache_age_ms": 0,
    }
    svc._cache_put("overview:bundle:f7", sentinel)

    fresh = svc.get_overview_bundle(bypass_cache=False)
    assert fresh.cached is True
    assert fresh.cache_age_ms >= 0

    # Cleaning up test pollution
    svc._overview_cache.clear()


def test_overview_bundle_bypass_cache_drops_previous_entry(monkeypatch):
    """bypass_cache=True should always do a fresh build (cached=False)."""
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x",
    )
    svc._overview_cache.clear()
    from app.schemas.market_dashboard import (
        FxRateResponse, MarketSnapshot, RegionalBatchResponse,
        ForecastBaseBatchResponse,
    )

    def fake_snapshot(group=None):
        return [MarketSnapshot(id="x", name="X", group="upstream",
                               price=1.0, date="2026-08-01",
                               change_pct=0.0, note="ok")]

    monkeypatch.setattr(svc, "get_market_snapshot", fake_snapshot)
    monkeypatch.setattr(svc, "get_regional_prices_batch",
                        lambda pids: RegionalBatchResponse(items={}, errors={}))
    monkeypatch.setattr(svc, "get_forecast_base_batch",
                        lambda pids, day=7: ForecastBaseBatchResponse(
                            day=7, base_by_product={}))

    fresh = svc.get_overview_bundle(bypass_cache=True)
    # bypass_cache=True means the new bundle isn't itself a cache HIT,
    # even though subsequent get_overview_bundle(False) calls would be.
    assert fresh.cached is False
    assert fresh.cache_age_ms == 0
    svc._overview_cache.clear()
