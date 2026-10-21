"""CadRevision — engineering-change revision store (roadmap P3).

One row per proposed change: before/after measurement snapshots, the
deterministic impact report, and an approval state machine
(draft → pending_approval → released | rejected). Rows cascade with the
conversation (hard-delete rule, same as CadBuildContract).
"""

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class CadRevision(TimestampedBase):
    __tablename__ = "cad_revisions"

    conversation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revision_no: Mapped[int] = mapped_column(default=1)
    parent_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    change_summary: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # draft | pending_approval | released | rejected
    status: Mapped[str] = mapped_column(String(30), default="pending_approval")
    before_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    impact_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approved_by_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
