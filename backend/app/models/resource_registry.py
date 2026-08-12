"""Unified Resource Registry — one row per indexed project resource.

Registry entries give the agent (and the Data Map UI) a single,
visibility-tiered view over everything connected to a project: databases,
files, reports, project memories, decisions, and derived entities.

Visibility tiers:
- ``project``      — visible to all project members (default)
- ``user_private`` — visible only to ``owner_user_id`` (and org admins)
- ``org``          — visible org-wide

Populated by ``services/knowledge_graph/registry_indexer.py`` upserts;
read via ``routers/project_catalog.py``. Flag-gated by
``KG_RESOURCE_REGISTRY_ENABLED``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class ResourceRegistry(TimestampedBase):
    __tablename__ = "resource_registry"

    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # database / file / report / memory / decision / entity
    resource_id: Mapped[str | None] = mapped_column(String(191), nullable=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    entities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default="project"
    )  # project / user_private / org
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending / indexing / ready / error
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "project_id", "resource_type", "resource_id",
            name="uq_resource_registry",
        ),
    )
