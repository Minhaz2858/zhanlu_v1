"""Per-session state for the session-cached re-export feature.

One row per chat session; ``session_id`` is the sole primary key (the
inherited ``id`` is overridden to a non-PK, nullable column so the table does
not end up with a composite primary key). The row tracks the most recent
cached data execution and a monotonic execution counter so the agent can
deduplicate identical re-export tool calls within a session.
"""

from typing import Optional

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class SessionState(TimestampedBase):
    __tablename__ = "session_states"

    # WATCH ITEM: TimestampedBase declares `id` as PK. Override it to a
    # non-PK, nullable column so session_id is the ONLY primary key and the
    # migration (single PK on session_id) matches the mapper exactly.
    id: Mapped[Optional[str]] = mapped_column(
        String(36), primary_key=False, nullable=True
    )
    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_execution_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    last_tool_name: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    last_data_signature: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    execution_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Re-declared to match the migration: base org_id/app_id are String(36)
    # NOT NULL with server defaults; SessionState rows are org-scoped via the
    # session, so these are nullable and wider here.
    org_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    app_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
