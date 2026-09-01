"""Full-Stack Dashboard App model.

Replaces the legacy SQL-widget ``Dashboard`` model for the new pipeline:
each row is a deployable FastAPI sub-app (Jinja2-generated) with a persisted
``DashboardSpec``, a served URL, and a lifecycle status.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Text, Index, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class DashboardApp(TimestampedBase):
    """One row per deployed full-stack dashboard application."""

    __tablename__ = "dashboard_apps"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    datasource_kb_id: Mapped[str] = mapped_column(String(120), nullable=False)
    design_system_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    spec: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # full DashboardSpec
    status: Mapped[str] = mapped_column(
        String(20), default="building", nullable=False
    )  # "building" | "running" | "stopped" | "error"
    app_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    refresh_interval_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    project_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    # Phase 2 (T4): when the underlying data last changed (bumped by the
    # realtime poller when a query hash changes). NULL until the first change
    # event, so newly-created dashboards don't show as unread before any data
    # has refreshed.
    last_data_change_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Phase 2 (T4): when the current user last opened/viewed the dashboard.
    # Combined with last_data_change_at → `unread` flag for the My Files badge.
    viewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Phase 2 (T5): the chat conversation (agent_conversations.id) that created
    # this dashboard. Powers My Files "Open in chat" — resumes the exact thread
    # that built the app. NULL for dashboards created outside a chat.
    chat_thread_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    # Phase 2 (T10): visibility scope — "personal" (creator only) or "company"
    # (visible to the whole org). Defaults to personal per project convention.
    scope: Mapped[str] = mapped_column(String(20), default="personal", nullable=False)

    __table_args__ = (
        Index("ix_dashboard_apps_org_slug", "org_id", "slug", unique=True),
    )
