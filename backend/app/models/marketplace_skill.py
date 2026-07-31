"""MarketplaceSkill — community-published skills in the public marketplace.

Separate from the `tools` table to keep marketplace metadata
(download count, ratings, signatures) isolated from agent-bound tools.
"""

from sqlalchemy import String, Text, Integer, Float, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class MarketplaceSkill(TimestampedBase):
    __tablename__ = "marketplace_skills"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    version: Mapped[str] = mapped_column(String(50), default="1.0.0", nullable=False)
    publisher_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    publisher_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skill_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Metrics
    download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    ratings_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Verification
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Author email (for verification)
    author_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
