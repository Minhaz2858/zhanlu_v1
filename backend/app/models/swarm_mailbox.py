"""SwarmMailboxMessage model — persistent mailbox for swarm team members.

Stores inter-agent messages so they survive ``SwarmCoordinator`` /
``TeamRegistry`` restarts. The previous in-memory ``TeamMember.mailbox``
list lost all messages whenever the singleton registry was reset; this
table makes the mailbox durable.

Schema mirrors :class:`app.services.swarm.MailboxMessage` so existing
swarm code that uses the dataclass shape keeps working — the model has
``id``, ``team_id``, ``sender``, ``recipient``, ``content``,
``summary``, ``timestamp``, ``read`` fields.
"""

from sqlalchemy import String, Text, Boolean, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class SwarmMailboxMessage(TimestampedBase):
    __tablename__ = "swarm_mailbox_messages"

    team_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sender: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    recipient: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Priority queue (Phase 5). Higher = dequeued first. 0 = normal priority
    # (preserves FIFO ordering among equal-priority messages by timestamp).
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)

    def to_dict(self) -> dict:
        d = super().to_dict()
        return d
