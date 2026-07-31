"""Pydantic response shapes for the EDIA market dashboard mirror.

Field names mirror EDIA's frontend interfaces
(MarketSnapshotItem, RegionalPriceData, etc.) so the ported EDIA React
components can consume the payload without remapping.
"""
from typing import Optional
from pydantic import BaseModel, Field


class MarketSnapshot(BaseModel):
    id: str
    name: str
    group: str
    price: Optional[float] = None
    date: Optional[str] = None
    unit: str = "CNY/mt"
    change_pct: Optional[float] = None
    weekly_change_pct: Optional[float] = None
    daily_gap_value: Optional[float] = None
    weekly_gap_value: Optional[float] = None
    daily_reference_date: Optional[str] = None
    weekly_reference_date: Optional[str] = None
    data_source_detail: Optional[str] = None
    note: Optional[str] = None  # set when mapping is missing


class PriceHistoryPoint(BaseModel):
    date: str
    price: float
    volume: Optional[float] = None


class RegionalPriceRegion(BaseModel):
    price_type: str
    label: str
    is_internal: bool = False
    price: float
    change_pct: Optional[float] = None
    date: str
    days_stale: int = 0


class RegionalPriceData(BaseModel):
    product_id: str
    regions: list[RegionalPriceRegion]
    spread_cny: Optional[float] = None  # v1: not computed; left null
    spread_level: Optional[str] = None  # v1: not computed
    price_realization_pct: Optional[float] = None  # v1: not computed
    note: Optional[str] = None


class NewsListItem(BaseModel):
    """v1 placeholder — real news requires EDIA's AI event pipeline."""
    event_id: str
    headline: str
    detected_at: str
    source_url: Optional[str] = None
    summary: Optional[str] = None
    group: Optional[str] = None


class NewsListResponse(BaseModel):
    items: list[NewsListItem] = Field(default_factory=list)
    note: Optional[str] = None


class EnabledResponse(BaseModel):
    enabled: bool
    reachable: bool
    reason: Optional[str] = None  # "not_configured" | "configured_unreachable" | "ok"
