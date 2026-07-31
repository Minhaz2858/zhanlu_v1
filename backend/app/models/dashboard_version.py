"""DashboardVersion — lightweight config snapshots enabling undo of edits."""
from typing import Optional

from sqlalchemy import String, Integer, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class DashboardVersion(TimestampedBase):
    __tablename__ = "dashboard_versions"

    dashboard_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dashboards.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Full snapshot of the dashboard definition + scalar fields at the time
    # this version was superseded: {"name","description","refresh_interval_seconds","definition"}.
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # agent|manual|undo
    summary: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
