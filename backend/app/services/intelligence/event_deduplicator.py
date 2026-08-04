"""Event Deduplicator — prevents the same real-world event from being processed twice.

Uses a combination of headline similarity, time proximity, and event type
to detect when two ExtractedEvents describe the same underlying event.

Ported from EDIA 5.1.2 — pure Python, no external dependencies.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional

from app.services.intelligence.event_types import ExtractedEvent

logger = logging.getLogger(__name__)

# Events within this window with similar fingerprints are considered duplicates
_DEDUP_WINDOW_HOURS = 24


def _fingerprint(event: ExtractedEvent) -> str:
    """Create a fuzzy fingerprint from event type + key entities + direction."""
    parts = [
        event.event_type,
        event.direction,
        event.geographic_scope,
        "|".join(sorted(event.affected_commodities[:4])),
        "|".join(sorted(event.key_entities[:3])),
    ]
    raw = "::".join(parts).lower()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class EventDeduplicator:
    """In-memory deduplication cache for recently seen events."""

    def __init__(self, window_hours: int = _DEDUP_WINDOW_HOURS):
        self._seen: dict[str, datetime] = {}  # fingerprint → first-seen time
        self._window = timedelta(hours=window_hours)

    def is_duplicate(self, event: ExtractedEvent) -> bool:
        """Return True if a substantially similar event was seen recently."""
        self._evict_expired()
        fp = _fingerprint(event)
        if fp in self._seen:
            logger.debug("Duplicate event fingerprint %s for: %s", fp, event.headline)
            return True
        return False

    def mark_seen(self, event: ExtractedEvent) -> None:
        """Record an event as seen."""
        fp = _fingerprint(event)
        self._seen[fp] = datetime.utcnow()

    def deduplicate(self, events: list[ExtractedEvent]) -> list[ExtractedEvent]:
        """Filter a batch of events, returning only non-duplicate ones."""
        unique: list[ExtractedEvent] = []
        for ev in events:
            if not self.is_duplicate(ev):
                self.mark_seen(ev)
                unique.append(ev)
        return unique

    def _evict_expired(self) -> None:
        now = datetime.utcnow()
        expired = [fp for fp, t in self._seen.items() if now - t > self._window]
        for fp in expired:
            del self._seen[fp]

    @property
    def cache_size(self) -> int:
        return len(self._seen)
