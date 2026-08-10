"""ResponseCacheEntry — semantic response cache (experience layer, Phase B).

Stores final assistant responses keyed by question embedding + data
version, so similar questions answered against the same data snapshot
can be replayed instantly instead of re-running the full tool loop.

Strict freshness guards: a cache hit requires the same agent, matching
scope, the same ``data_version``, a cosine similarity >= threshold, a
feedback score above the eviction floor, and a non-expired entry.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

# Cache scopes
CACHE_SCOPE_SHARED = "shared"  # data-driven reports — shared across users
CACHE_SCOPE_USER = "user"  # conversational answers — per-user

# Intents eligible for shared caching (data-driven report intents)
SHARED_CACHE_INTENTS = ("price_report", "market_analysis", "forecast_question", "comparison")

DEFAULT_CACHE_TTL_HOURS = 24
DEFAULT_CACHE_SIM_THRESHOLD = 0.92
FEEDBACK_EVICTION_FLOOR = -2.0


class ResponseCacheEntry(TimestampedBase):
    """A cached assistant response with its question embedding + data version."""

    __tablename__ = "response_cache_entries"

    agent_app_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(10), nullable=False, default=CACHE_SCOPE_SHARED)
    user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    intent_class: Mapped[str] = mapped_column(String(30), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    question_embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    response_content: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    data_version: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    feedback_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    @classmethod
    def ttl_from_now(cls, hours: int = DEFAULT_CACHE_TTL_HOURS) -> datetime:
        """UTC expiry timestamp ``hours`` from now."""
        return datetime.now(timezone.utc) + timedelta(hours=hours)
