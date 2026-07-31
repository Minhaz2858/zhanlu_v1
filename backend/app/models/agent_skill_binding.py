"""AgentSkillBinding — links an agent to a skill with version pinning.

Specifies which skills an agent can use, with explicit version pinning
and allow/block lists.  This replaces the loose `skills` JSON array on
AgentApp with a governed binding system.
"""

from typing import Optional

from sqlalchemy import String, Boolean, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class AgentSkillBinding(TimestampedBase):
    """A skill binding between an agent and a skill.

    Version pinning ensures reproducibility — an agent always uses the
    same skill version unless explicitly upgraded.
    """

    __tablename__ = "agent_skill_bindings"

    agent_app_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_apps.id"), nullable=False, index=True)
    skill_name: Mapped[str] = mapped_column(String(100), nullable=False)
    skill_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # None = latest

    # Binding config
    is_allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # True = allow, False = block
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # True = version locked
    config_override: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # Per-agent skill config

    # Usage tracking
    call_count: Mapped[int] = mapped_column(default=0, nullable=False)
    last_used_at: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
