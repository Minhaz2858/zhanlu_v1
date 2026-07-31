"""Pydantic schemas for the EDIA dashboard mirror."""
import pytest
from pydantic import ValidationError

from app.schemas.market_dashboard import (
    MarketSnapshot, PriceHistoryPoint, RegionalPriceRegion,
    RegionalPriceData, NewsListResponse, EnabledResponse,
)


def test_market_snapshot_minimum_fields():
    s = MarketSnapshot(
        id="naphtha",
        name="Japanese Naphtha",
        price=612.5,
        date="2026-07-30",
        change_pct=1.2,
        group="upstream",
    )
    assert s.unit == "CNY/mt"
    assert s.weekly_change_pct is None
    assert s.note is None


def test_market_snapshot_allows_nulls_for_unmapped():
    s = MarketSnapshot(
        id="crude_oil",
        name="Crude Oil",
        price=None,
        date=None,
        change_pct=None,
        group="upstream",
        note="table mapping not configured in v1",
    )
    assert s.price is None


def test_price_history_point_required():
    with pytest.raises(ValidationError):
        PriceHistoryPoint(date="2026-07-30")  # missing price


def test_regional_price_region_minimal():
    r = RegionalPriceRegion(
        price_type="east_china", label="华东", is_internal=False,
        price=9800.0, change_pct=None, date="2026-07-30", days_stale=0,
    )
    assert r.days_stale == 0


def test_news_list_response_v1_shape():
    r = NewsListResponse(items=[], note="...")
    assert r.items == []


def test_enabled_response_off():
    e = EnabledResponse(enabled=False, reachable=False, reason="not_configured")
    assert e.enabled is False
