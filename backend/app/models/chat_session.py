"""ChatSession model."""

from sqlalchemy import String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class ChatSession(TimestampedBase):
    __tablename__ = "chat_sessions"

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    project: Mapped[str | None] = mapped_column(String(255), nullable=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=True, index=True
    )
    starred: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    last_message_at: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Set by automation runs so the sidebar can show an unread indicator.
    # Cleared when the user opens the session (see ChatSessionContext.selectSession).
    unread: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)

    # ── Conversation / agent linkage (added by migration 029) ─────────
    # Persisted link to the AgentConversation this session was opened
    # with, so reopening a session can resume the same conv (avoiding
    # orphan convs and the "Recent Chats grows one row per message"
    # bug) and re-attach the same agent without prompting the user.
    #
    # Both columns are nullable: legacy ChatSession rows created before
    # migration 029 have no linked conv / agent, and new system-agent
    # sessions may never set agent_name.
    #
    # FK is ON DELETE SET NULL — deleting an AgentConversation must NOT
    # cascade-delete the chat history; the session just forgets which
    # conv it owned and the next send creates a new one.
    conversation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)