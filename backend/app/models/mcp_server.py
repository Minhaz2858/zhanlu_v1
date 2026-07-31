"""McpServer model — MCP server connections."""

from sqlalchemy import String, Text, Integer
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
