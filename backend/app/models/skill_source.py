"""SkillSource model — registered external skill sources (GitHub repos, JSON indexes, web pages)."""

from sqlalchemy import String, Text, Integer, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from typing import Optional
from datetime import datetime

from app.models.base import TimestampedBase


class SkillSource(TimestampedBase):
    __tablename__ = "skill_sources"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)  # github_repo | web_index | web_page
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    added_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_sync_status: Mapped[str] = mapped_column(String(20), default="never", nullable=False)
    last_sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    skill_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Visual branding for the source card on the Browse Marketplace tab.
    # `brand_color` is a hex string (`#RRGGBB`); `icon_emoji` is a single
    # emoji glyph. Both are optional — the API and the UI fall back to a
    # neutral gray + the first letter of the name when they're missing.
    brand_color: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    icon_emoji: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
