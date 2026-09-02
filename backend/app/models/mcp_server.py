"""McpServer model — MCP server connections (2026-spec aware).

``transport``: "stdio" | "http" | "streamable" (legacy alias).
``server_url``: for stdio, the raw ``command args...`` string; for http, the
remote base URL (``base_url`` mirrors it for clarity).
``capabilities``/``oauth_token_json`` hold protocol-negotiation and OAuth
state. Tokens are stored here (encrypted at rest is a follow-up) and never
exposed through the API.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class McpServer(TimestampedBase):
    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    server_url: Mapped[str] = mapped_column(String(500), nullable=False)
    transport: Mapped[str | None] = mapped_column(String(50), nullable=True, default="streamable")
    status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="disconnected")
    tools_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    resources_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)

    # ── 2026-spec additions ─────────────────────────────────────────────
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    auth_type: Mapped[str | None] = mapped_column(String(50), nullable=True, default="none")
    capabilities: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    oauth_token_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict:
        """Public serialization — never includes OAuth tokens."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "server_url": self.server_url,
            "base_url": self.base_url or self.server_url,
            "transport": self.transport or "streamable",
            "status": self.status,
            "tools_count": self.tools_count or 0,
            "resources_count": self.resources_count or 0,
            "auth_type": self.auth_type or "none",
            "capabilities": self.capabilities or {},
            "last_probed_at": self.last_probed_at.isoformat() if self.last_probed_at else None,
            "created_date": self.created_date.isoformat() if self.created_date else None,
        }
