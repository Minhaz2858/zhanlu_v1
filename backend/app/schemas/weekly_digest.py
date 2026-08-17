"""Pydantic schemas for the Weekly Summary & Forecast Digest API."""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class WeeklyReportProductResponse(BaseModel):
    """Single product within a weekly report."""
    id: str
    product_key: str
    product_name_zh: str
    product_name_en: str
    report_order: int
    current_price: Optional[float] = None
    change_pct_7d: Optional[float] = None
    price_12mo: Optional[list[dict]] = Field(default_factory=list)
    price_30d: Optional[list[dict]] = Field(default_factory=list)
    market_analysis: Optional[str] = None
    forecast_7d: Optional[dict] = None
    forecast_30d: Optional[dict] = None
    ai_decision: Optional[str] = None

    model_config = {"from_attributes": True}


class WeeklyReportCardResponse(BaseModel):
    """Summary card for a weekly report (used in list view)."""
    id: str
    week_start: date
    week_end: date
    title_zh: str
    title_en: str
    product_count: int
    status: str
    error_msg: Optional[str] = None
    market_overview: Optional[str] = None
    executive_summary: Optional[str] = None
    generated_at: Optional[datetime] = None
    created_date: Optional[datetime] = None

    model_config = {"from_attributes": True}


class WeeklyReportDetailResponse(WeeklyReportCardResponse):
    """Full report with all product data."""
    products: list[WeeklyReportProductResponse] = Field(default_factory=list)


class WeeklyReportListResponse(BaseModel):
    """Paginated list of weekly report cards."""
    items: list[WeeklyReportCardResponse] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


class WeeklyDigestTriggerResponse(BaseModel):
    """Response for the manual trigger endpoint."""
    status: str
    card_id: Optional[str] = None
    message: str
