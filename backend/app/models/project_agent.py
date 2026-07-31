"""ProjectAgent — many-to-many association between Project and AgentApp.

An agent can belong to multiple projects simultaneously. An agent's runtime
context (knowledge bases, files, automations) is the UNION of everything its
member projects provide.

The legacy ``AgentApp.project_id`` column is preserved as the agent's
"primary/home project" for backward compatibility — it's denormalized from
this table and updated whenever membership changes.
"""

from sqlalchemy import String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class ProjectAgent(TimestampedBase):
    __tablename__ = "project_agents"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=False, index=True,
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_apps.id"), nullable=False, index=True,
    )
    # "primary" — the agent's home project (synced with AgentApp.project_id)
    # "member" — additional project membership beyond the primary one
    role: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default="member",
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "agent_id", "org_id", "app_id",
            name="uq_project_agents_project_agent_org_app",
        ),
    )
