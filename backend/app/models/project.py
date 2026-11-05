"""Project model."""

from typing import TYPE_CHECKING, Optional

from sqlalchemy import String, Text, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TimestampedBase

if TYPE_CHECKING:
    from app.models.project_memory import ProjectMemory
    from app.models.llm_model import LlmModel


class Project(TimestampedBase):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="active")
    # ``is_system`` projects are platform-shipped (e.g. a system default) and
    # visible to all users in the list endpoint — see entity_service.py
    # where ``(created_by_id == owner_id) OR created_by_id IS NULL``
    # includes system-owned rows.  Mirrors the AgentApp.is_system pattern.
    # Column is added at runtime via ensure_system_agents() schema-ensure.
    is_system: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # resource_type is stamped server-side at creation from the creator's
    # role (admin→'company', user→'personal').  Immutable — clients can
    # never change it via PUT (added to _IMMUTABLE_FIELDS).
    resource_type: Mapped[str] = mapped_column(
        String(20), default="personal", nullable=False, index=True,
    )

    # Hierarchical LLM config — FK to llm_models catalog (gated by HIERARCHICAL_LLM_ENABLED)
    llm_model_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("llm_models.id"), nullable=True, index=True,
    )
    llm_model: Mapped[Optional["LlmModel"]] = relationship("LlmModel", foreign_keys=[llm_model_id])

    # P1 (2026-10-31): department LABEL on the project container — feeds
    # agent-discovery scoping ONLY (docs/architecture/
    # zhanlu-enterprise-agent-discovery-and-routing.md §4.1). Never used for
    # data authorization: KB/RLS/policy scoping is unchanged.
    department: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True, index=True,
    )

    # P1B.3 (2026-11-01): optional link to the AUTHORITATIVE department
    # model (OrgUnit, unit_type department|team). Plain id, no hard FK —
    # org consistency and unit_type are validated by the entity API when
    # linking. Never auto-assigned; never used for authorization (the
    # legacy `department` label stays for discovery-routing compat).
    department_org_unit_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True,
    )

    # Reverse relationship populated by ProjectMemory.project
    project_memories: Mapped[list["ProjectMemory"]] = relationship(
        "ProjectMemory",
        back_populates="project",
        cascade="all, delete-orphan",
    )
