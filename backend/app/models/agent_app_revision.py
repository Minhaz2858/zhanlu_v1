"""AgentAppRevision — immutable publish snapshots (ADLC P0-3).

Every publish bumps ``AgentApp.version`` and writes one revision row with a
full snapshot of the agent's definition. Rollback restores a snapshot and
bumps again (writing a new revision), so revision history is append-only —
the audit trail IBM's ADLC "well-governed catalog" requires.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class AgentAppRevision(TimestampedBase):
    __tablename__ = "agent_app_revisions"

    agent_app_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Full snapshot of the agent definition at publish time
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    published_by_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # "publish" | "rollback"
    action: Mapped[str] = mapped_column(String(20), default="publish", nullable=False)
    status_at_publish: Mapped[str | None] = mapped_column(String(50), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_app_id": self.agent_app_id,
            "version": self.version,
            "note": self.note,
            "action": self.action,
            "status_at_publish": self.status_at_publish,
            "published_by_id": self.published_by_id,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "created_date": self.created_date.isoformat() if self.created_date else None,
        }
