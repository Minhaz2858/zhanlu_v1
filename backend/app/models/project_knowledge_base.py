"""ProjectKnowledgeBase — many-to-many association between Project and KnowledgeBase.

A connector (KnowledgeBase row: MySQL / PostgreSQL / JSON / SQLite) can belong
to multiple projects simultaneously. A project's runtime context (knowledge
bases) is the UNION of:
  * KBs whose home project is this project (``KnowledgeBase.project_id``), and
  * KBs linked to this project via this table.

Mirrors ``ProjectAgent``: the legacy ``KnowledgeBase.project_id`` column is
preserved as the KB's "primary/home project" for backward compatibility;
membership beyond the home project lives here. The unique constraint makes
attach idempotent (re-attach is a no-op, not an IntegrityError surfaced to
users).
"""

from sqlalchemy import String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class ProjectKnowledgeBase(TimestampedBase):
    __tablename__ = "project_knowledge_bases"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=False, index=True,
    )
    knowledge_base_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id"), nullable=False, index=True,
    )
    # "home" — synced with KnowledgeBase.project_id (backfill / move-home)
    # "member" — additional project membership beyond the home one
    role: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default="member",
    )
    created_by_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "project_id", "knowledge_base_id", "org_id", "app_id",
            name="uq_project_kbs_project_kb_org_app",
        ),
    )
