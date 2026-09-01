"""Intelligence domain models — market events and ingestion status.

Adapted to zhanlu's SQLAlchemy + TimestampedBase pattern.

Tables:
    intelligence_events            — detected market events with causal metadata
    intelligence_ingestion_status — health tracking for background news ingestion
"""

from typing import Optional

from sqlalchemy import String, Text, Integer, Float, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class IntelligenceEvent(TimestampedBase):
    """A market-moving event detected from news, RSS, or manual submission.

    Stores the full ExtractedEvent payload plus review/moderation fields.
    ``event_id`` is a separate unique key from the UUID ``id`` inherited
    from TimestampedBase — it's the ``evt_<hex12>`` identifier
    used for deduplication and cross-referencing.
    """

    __tablename__ = "intelligence_events"

    # event_id (evt_xxxxxxxxxxxx format)
    event_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    affected_commodities: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    direction: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    magnitude_estimate: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    certainty: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    time_horizon: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    geographic_scope: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    relevance_to_c5_c9: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    key_entities: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    key_information: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    causal_chain_hint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_credibility: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    full_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_quote: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detected_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    impact_magnitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    relevance_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="approved", index=True
    )
    usefulness_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class IntelligenceIngestionStatus(TimestampedBase):
    """Tracks the health and freshness of background intelligence ingestion agents.

    One row per (agent_name, org_id). The ``last_success_at`` timestamp is
    used by the freshness checker to detect stale data feeds.
    """

    __tablename__ = "intelligence_ingestion_status"

    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    running: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cycle_in_progress: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    scan_interval_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_started_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_success_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_cycle_duration_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_articles_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_events_extracted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_events_stored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_run_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
