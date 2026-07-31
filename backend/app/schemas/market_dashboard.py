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


# ── Phase 7 (verbatim port): batch + new endpoints ─────────────────

class RegionalBatchResponse(BaseModel):
    """Batch response for /regional-batch — one RegionalPriceData per product."""
    items: dict[str, RegionalPriceData] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


class ForecastBaseBatchResponse(BaseModel):
    """Batch response for /forecast-base-batch — 7-day-ahead forecast prices."""
    day: int
    base_by_product: dict[str, Optional[float]] = Field(default_factory=dict)


class FxRateResponse(BaseModel):
    """USD-CNY FX rate (v1: static)."""
    price: Optional[float]
    unit: str
    as_of_date: Optional[str]
    series_product_id: str
    series_price_type: str
    data_source: str
    available: bool


class EventItem(BaseModel):
    """An intelligence event from EDIA's /api/events/latest (verbatim shape)."""
    event_id: str
    headline: str
    detected_at: str
    source_url: Optional[str] = None
    event_type: Optional[str] = None
    direction: Optional[str] = None
    impact_magnitude: Optional[float] = None
    certainty: Optional[str] = None
    source_credibility: Optional[str] = None
    usefulness_score: Optional[float] = None
    relevance_score: Optional[float] = None
    affected_commodities: list[str] = Field(default_factory=list)
    causal_chain_hint: Optional[str] = None
    raw_quote: Optional[str] = None
    full_text: Optional[str] = None
    relevance_to_c5_c9: Optional[str] = None
    source_label: Optional[str] = None
    summary: Optional[str] = None
    quality_tier: Optional[str] = None
    time_horizon: Optional[str] = None
    geographic_scope: Optional[str] = None
    key_entities: list[str] = Field(default_factory=list)


class EventsResponse(BaseModel):
    """Response for /events/latest."""
    events: list[EventItem] = Field(default_factory=list)
    count: int = 0
