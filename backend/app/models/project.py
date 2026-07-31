"""Project model."""

from typing import TYPE_CHECKING

from sqlalchemy import String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TimestampedBase

if TYPE_CHECKING:
    from app.models.project_memory import ProjectMemory


class Project(TimestampedBase):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="active")
    # ``is_system`` projects are platform-shipped (e.g. "Ecisco BI") and
    # visible to all users in the list endpoint — see entity_service.py
    # where ``(created_by_id == owner_id) OR created_by_id IS NULL``
    # includes system-owned rows.  Mirrors the AgentApp.is_system pattern.
    # Column is added at runtime via ensure_system_agents() schema-ensure.
    is_system: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Reverse relationship populated by ProjectMemory.project
    project_memories: Mapped[list["ProjectMemory"]] = relationship(
        "ProjectMemory",
        back_populates="project",
        cascade="all, delete-orphan",
    )
