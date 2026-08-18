"""ArtifactEvent model — append-only usage instrumentation for PPTX decks.

Phase 5 of the PPT Professional Upgrade.  Stores only structural metadata
about deck lifecycle events (generated / edited / downloaded) — never slide
content — so the weekly usage digest can run as plain SQL without exposing
user data.

Event types (enum-like strings, kept narrow on purpose):
    * ``deck_generated``  — a deck was planned + rendered.
    * ``deck_edited``     — a deck edit (restyle / add / remove / reorder …).
    * ``deck_downloaded`` — a deck was exported (pptx / pdf / image).

``metadata_json`` is a flexible payload (e.g. profile, theme, slide count,
edit kind).  It must never contain slide text.
"""

from __future__ import annotations

from sqlalchemy import String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column
from typing import Optional

from app.models.base import TimestampedBase


# Canonical event types.  Kept as a tuple (not an enum) so the logging helper
# accepts arbitrary strings from callers without import friction.
ARTIFACT_EVENT_TYPES = (
    "deck_generated",
    "deck_edited",
    "deck_downloaded",
)


class ArtifactEvent(TimestampedBase):
    __tablename__ = "artifact_events"

    # The artifact (deck) this event concerns.  Not a hard FK — events outlive
    # artifacts and we never want a delete to cascade into analytics.
    artifact_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    # Optional owner for per-user slicing.  Indexed but nullable.
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    # Flexible, content-free metadata (profile, theme, slide count, edit kind…).
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Composite index for the weekly digest query (type + time range).
        Index("ix_artifact_events_type_created", "event_type", "created_date"),
    )
