"""Organization model — the top-level multi-tenant isolation boundary.

Every other model inherits ``org_id`` from ``TimestampedBase``; this table is
the source of truth for org metadata, plan, and status.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class Organization(TimestampedBase):
    """An organization (tenant) in the Zhanlu platform."""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    plan: Mapped[str] = mapped_column(String(50), default="free", nullable=False)
    settings_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
