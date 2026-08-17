"""AlertProductBaseline — cached LLM-judged weekly volatility baseline per product.

Used by the Smart Alerts evaluator to adapt trigger thresholds to each
product's typical 7-day price movement. Populated lazily on cache miss,
refreshed after ``BASELINE_TTL_SECONDS`` (7 days), seeded with a safe
default (2.5%) when the LLM call fails so the scan never blocks.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Float, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AlertProductBaseline(Base):
    __tablename__ = "alert_product_baselines"

    product_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    baseline_pct: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    model_used: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "baseline_pct": self.baseline_pct,
            "reasoning": self.reasoning,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
            "model_used": self.model_used,
        }
