"""Report model — generated DOCX reports."""

from sqlalchemy import String, Text, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class Report(TimestampedBase):
    __tablename__ = "reports"

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="generating")
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=True, index=True
    )
    project: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    read: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    pinned: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
