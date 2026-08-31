"""ChatShare model — token-based public share of a chat session.

Kimi/GPT-style conversation sharing: the owner creates a share for a
ChatSession and gets back an unguessable token. The read-only public
page lives at ``/share/c/<token>`` (no auth required) and is served
from the ``chat_shares`` row.

``created_by_id`` (inherited from TimestampedBase) = the sharing user.
``expires_at`` is nullable — a null means the share never expires.
"""

from datetime import datetime
from sqlalchemy import String, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class ChatShare(TimestampedBase):
    __tablename__ = "chat_shares"

    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="The shared chat session (chat_sessions.id).",
    )

    token: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True,
        doc="Unguessable share token (uuid4().hex).",
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        doc="Optional expiry; None = never expires.",
    )
