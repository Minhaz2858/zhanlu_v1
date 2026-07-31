"""MarketplaceRating — individual user ratings for marketplace skills."""

from sqlalchemy import String, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class MarketplaceRating(TimestampedBase):
    __tablename__ = "marketplace_ratings"

    marketplace_skill_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-5
    review: Mapped[str | None] = mapped_column(String(1000), nullable=True)
