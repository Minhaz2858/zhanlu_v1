"""AgentConversation model — for the SDK's agents module (skill_agent, agent_builder, automation_agent)."""

from datetime import datetime
from sqlalchemy import String, Text, JSON, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class AgentConversation(TimestampedBase):
    __tablename__ = "agent_conversations"

    agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    messages: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="active")
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)

    # Project association — set when this conversation belongs to a Project.
    # Nullable for system-agent conversations (agent_builder, skill_agent) that
    # are not tied to a specific user project.
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=True, index=True
    )

    # The ONE dashboard bound to this conversation (1:1). Set by
    # create_dashboard when invoked from chat; subsequent update_dashboard
    # calls resolve this binding automatically. ON DELETE SET NULL so
    # deleting a dashboard forgets the binding without destroying the chat.
    dashboard_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("dashboards.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
