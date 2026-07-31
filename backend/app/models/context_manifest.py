"""ContextManifest — standalone structured context records.

Stores reusable context blocks with versioning, type tags, expiration,
and access counters.  Used by the Synexia cognitive pipeline to inject
relevant domain knowledge, policies, or facts into the LLM prompt.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Boolean, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class ContextManifest(TimestampedBase):
    """A standalone, versioned context record for prompt injection."""

    __tablename__ = "context_manifests"

    # Identity
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    context_type: Mapped[str] = mapped_column(String(50), nullable=False)
    version: Mapped[str] = mapped_column(String(50), default="1.0.0", nullable=False)

    # Content
    content_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    content_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    source_ref: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Relevance
    tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Usage stats
    access_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_accessed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
