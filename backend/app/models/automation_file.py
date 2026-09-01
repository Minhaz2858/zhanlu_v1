"""AutomationFile model — file output of an automation execution.

Each execution can produce one or more files (PPTX, DOCX, PDF, image, CSV,
JSON, etc.). Files are stored on disk under ``uploads/automation/<id>/`` and
served via the existing /api/uploads static mount.
"""

from sqlalchemy import String, Integer, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class AutomationFile(TimestampedBase):
    __tablename__ = "automation_files"

    # Parent execution.
    execution_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("automation_executions.id"), nullable=False, index=True
    )
    # Parent task (denormalized for fast filtering in My Space without join).
    automation_task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("automation_tasks.id"), nullable=False, index=True
    )
    # Display name (e.g. "C5 Product Weekly Price Report").
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    # File extension (pptx, docx, pdf, html, json, csv, png, …).
    file_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # File size in bytes.
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # URL path relative to the backend root (e.g. /api/uploads/automation/.../report.pptx).
    file_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Absolute on-disk path (used for streaming downloads; not exposed to frontend).
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # MIME type for content-type header on download.
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    read: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    pinned: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
