"""ExternalAgent model — registry of remote A2A agents Zhanlu can delegate to.

IBM agent-protocols workstream #1 (2026-10-08): the A2A outbound client
(``app/services/a2a/client.py``) existed but nothing could invoke it.
This registry gives the ``invoke_external_agent`` tool a durable directory
of remote agents: their JSON-RPC endpoint, bearer token, capabilities, and
probe state (mirrors ``McpServer`` conventions — tokens stored here,
encrypted-at-rest is a follow-up, never exposed through the API).
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class ExternalAgent(TimestampedBase):
    __tablename__ = "external_agents"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-RPC endpoint for tasks/send (commonly .../a2a)
    endpoint_url: Mapped[str] = mapped_column(String(500), nullable=False)
    # Optional discovery document URL (agent card); probed for capabilities
    card_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Bearer token for the remote agent's server-controlled auth (A2A step 2)
    auth_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    capabilities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    timeout_s: Mapped[int | None] = mapped_column(Integer, nullable=True, default=60)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="unknown")
    last_probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Optional per-user/agent scoping (McpServer.granted_agents convention)
    granted_agents: Mapped[list | None] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> dict:
        """Public serialization — never includes auth_token."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "endpoint_url": self.endpoint_url,
            "card_url": self.card_url,
            "enabled": bool(self.enabled),
            "capabilities": self.capabilities or [],
            "timeout_s": self.timeout_s or 60,
            "status": self.status or "unknown",
            "last_probed_at": self.last_probed_at.isoformat() if self.last_probed_at else None,
            "last_task_id": self.last_task_id,
            "granted_agents": self.granted_agents or [],
            "created_date": self.created_date.isoformat() if self.created_date else None,
        }
