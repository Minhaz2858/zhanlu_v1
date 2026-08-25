"""Session-cached data-execution record.

Caches the raw result of an expensive tool call (e.g. ``ask_data_agent``)
per session so a follow-up turn in the same chat can reuse it instead of
re-running the query. Records are short-lived (``expires_at``) and
best-effort: persistence failures are logged, never raised into the tool
loop.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class DataExecution(TimestampedBase):
    """One cached tool execution, keyed by (session_id, tool_name).

    ``id`` deliberately overrides the base ``String(36)`` id with a wider
    ``String(64)`` so records use the plan-mandated ``evt_<hex>`` prefix.
    """

    __tablename__ = "data_executions"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: "evt_" + uuid.uuid4().hex[:12],
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    args: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    result: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    summary_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """Return True when the cache entry is past its TTL.

        ``None`` expires_at (permanent entry) is never expired. Naive
        datetimes are treated as UTC — required for SQLite round-trips,
        which drop tzinfo even when a timezone-aware value is written.
        """
        if self.expires_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        if self.expires_at.tzinfo is None:
            self.expires_at = self.expires_at.replace(tzinfo=timezone.utc)
        return self.expires_at <= current
