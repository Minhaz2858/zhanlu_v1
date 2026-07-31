"""App / Workspace model — a logical application boundary within an organization.

An organization can contain multiple apps.  ``app_id`` on every model (inherited
from ``TimestampedBase``) scopes records to a specific app within the org.
"""

from typing import Optional

from sqlalchemy import String, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class AppWorkspace(TimestampedBase):
    """A logical application workspace within an organization."""

    __tablename__ = "app_workspaces"
    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_app_workspaces_org_slug"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    config_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
