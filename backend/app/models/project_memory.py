"""ProjectMemory model — shared memory scoped to a Project.

All agents operating within the same project contribute to and read from
this shared memory, providing continuity across agent boundaries.
"""

from datetime import datetime

from sqlalchemy import String, Text, Integer, ForeignKey, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TimestampedBase


class ProjectMemory(TimestampedBase):
    __tablename__ = "project_memories"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=False, index=True,
    )
    agent_app_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_apps.id"), nullable=True,
        comment="Contributing agent; NULL = user-contributed entry",
    )
    entry_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="fact",
        comment="fact | decision | artifact_ref | conversation_summary | data_insight",
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
        comment="SHA-256 hex digest for deduplication",
    )
    importance: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    source_conversation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True,
    )
    source_artifact_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True,
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None,
    )

    # Relationships
    project: Mapped["Project"] = relationship("Project", back_populates="project_memories")
