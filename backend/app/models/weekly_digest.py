"""Weekly Summary & Forecast digest models.

Two tables store the pre-generated weekly reports:
- ``weekly_report_cards`` — one row per report generation run (week range, status)
- ``weekly_report_products`` — one row per product per report (price data, LLM analysis)
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import String, Date, DateTime, Float, Integer, Text, ForeignKey, Index, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampedBase


class WeeklyReportCard(TimestampedBase):
    """One row per weekly report generation run."""

    __tablename__ = "weekly_report_cards"

    week_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    week_end: Mapped[date] = mapped_column(Date, nullable=False)
    title_zh: Mapped[str] = mapped_column(String(200), nullable=False)
    title_en: Mapped[str] = mapped_column(String(200), nullable=False)
    product_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="generating", nullable=False
    )  # "generating" | "ready" | "failed"
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    market_overview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    executive_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_weekly_report_cards_org_week", "org_id", "week_start", unique=True),
    )


class WeeklyReportProduct(TimestampedBase):
    """One row per product per weekly report."""

    __tablename__ = "weekly_report_products"

    report_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("weekly_report_cards.id"), nullable=False, index=True
    )
    product_key: Mapped[str] = mapped_column(String(50), nullable=False)
    product_name_zh: Mapped[str] = mapped_column(String(100), nullable=False)
    product_name_en: Mapped[str] = mapped_column(String(100), nullable=False)
    report_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Price snapshot
    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    change_pct_7d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Price history — JSON arrays of {date, price} objects
    price_12mo: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    price_30d: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # LLM-generated text sections
    market_analysis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ai_decision: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Forecast payload — JSON {base, bull, bear, confidence}
    forecast_7d: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    forecast_30d: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index(
            "ix_weekly_report_products_report_order",
            "report_id",
            "report_order",
        ),
    )
