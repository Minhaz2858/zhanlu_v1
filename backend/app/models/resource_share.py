"""ResourceShare model — polymorphic ACL for sharing resources between users.

Owners can share their resources (Projects, AgentApps) with other users
for View + Use access.  Edit/delete/re-share remain owner-only.

``created_by_id`` (inherited from ``TimestampedBase``) serves as
``shared_by_user_id`` — the user who granted the share.
"""

from sqlalchemy import String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class ResourceShare(TimestampedBase):
    __tablename__ = "resource_shares"

    # Polymorphic target: 'project' | 'agent'
    resource_type: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True,
        doc="Entity type: 'project' or 'agent'.",
    )

    resource_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True,
        doc="UUID of the shared resource (projects.id or agent_apps.id).",
    )

    shared_with_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id"),
        nullable=False,
        index=True,
        doc="User who received the share grant.",
    )

    access_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="use",
        doc="'use' = view + run agents; 'view' reserved for future read-only share.",
    )

    # created_by_id (inherited from TimestampedBase) = shared_by_user_id
    # org_id, app_id, is_deleted, created_date, updated_date — all inherited
