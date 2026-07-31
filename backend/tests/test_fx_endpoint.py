"""Tests for /api/market-dashboard/fx/usd-cny endpoint (Task 4)."""
from app.services.market_dashboard_service import get_fx_rate


def test_fx_rate_returns_static_value():
    r = get_fx_rate()
    assert r.price == 7.20
    assert r.unit == "CNY/USD"
    assert r.series_product_id == "usd_cny"
    assert r.series_price_type == "spot"
    assert r.available is True
    assert r.data_source == "static_fallback"
    assert r.as_of_date is not None
