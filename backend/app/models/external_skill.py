"""ExternalSkill model — cached skill catalog entries from external sources."""

from sqlalchemy import String, Text, Integer, JSON, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from typing import Optional
from datetime import datetime

from app.models.base import TimestampedBase


class ExternalSkill(TimestampedBase):
    __tablename__ = "external_skills"

    source_id: Mapped[str] = mapped_column(String(36), ForeignKey("skill_sources.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="general")
    version: Mapped[str] = mapped_column(String(50), nullable=False, default="1.0.0")
    author: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    skill_md: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    github_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    install_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
