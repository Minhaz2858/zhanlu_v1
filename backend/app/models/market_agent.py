"""MarketAgent model — marketplace agents available for subscription."""

from sqlalchemy import String, Text, Integer, Float, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class MarketAgent(TimestampedBase):
    __tablename__ = "market_agents"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    capabilities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True, default=0.0)
    subscribers: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
