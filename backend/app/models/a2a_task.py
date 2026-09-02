"""A2A task model — external agent-to-agent delegation records."""

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

A2A_STATUSES = (
    "submitted",
    "working",
    "input-required",
    "completed",
    "failed",
    "canceled",
)


class A2aTask(TimestampedBase):
    __tablename__ = "a2a_tasks"

    agent_app_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    external: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="submitted", index=True)
    message_parts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
