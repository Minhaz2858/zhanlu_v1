"""RemovedCuratedUrl — tombstone for curated source URLs the user
explicitly deleted.

When the user clicks "Delete" on a default (curated) source, the
destructive action hard-deletes the source row AND its skills. Without
a tombstone, the next call to ``seed_curated_sources`` would re-create
the row (the seed only checks "does a row with this URL exist?" — and
after a hard delete, no row exists, so the seed happily re-creates
one). The user reported this on 2026-07-29: "after refresh it showing
again it's not working how i want it".

This table records the URL of any curated source the user has
explicitly removed, so the seed can skip those URLs on subsequent
runs. The user can later "restore" a removed source — that just
deletes the tombstone row, and the next list call re-creates the
source from the seed definition.

Note: non-default (user-added) sources don't need tombstones. They're
hard-deleted cleanly and there's no seed path that would re-create
them. Only the curated defaults (defined in
``skill_source_service.CURATED_SOURCES``) need this guard.
"""
from sqlalchemy import String, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from typing import Optional
from datetime import datetime, timezone

from app.models.base import TimestampedBase


class RemovedCuratedUrl(TimestampedBase):
    __tablename__ = "removed_curated_urls"

    url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True, index=True)
    removed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    # User who removed it. Nullable so the seed can record URLs even
    # when no user context is available (startup scripts, tests).
    removed_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
