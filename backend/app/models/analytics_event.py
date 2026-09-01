"""AnalyticsEvent model — stores analytics tracking events from the frontend."""

from sqlalchemy import String, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from typing import Optional

from app.models.base import TimestampedBase


class AnalyticsEvent(TimestampedBase):
    __tablename__ = "analytics_events"

    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
