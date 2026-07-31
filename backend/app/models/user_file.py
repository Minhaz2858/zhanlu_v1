"""UserFile model — uploaded and AI-generated files."""

from sqlalchemy import String, Integer, Boolean, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class UserFile(TimestampedBase):
    __tablename__ = "user_files"

    name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True, default="upload")
    resource_kind: Mapped[str | None] = mapped_column(String(100), nullable=True)
    project: Mapped[str | None] = mapped_column(String(255), nullable=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=True, index=True
    )
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    slides: Mapped[list | None] = mapped_column(JSON, nullable=True)
    read: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    pinned: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
