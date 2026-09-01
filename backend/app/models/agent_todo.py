"""AgentTodo model — session-scoped task planning and progress tracking.

Each conversation has one AgentTodo row containing a JSON array of items.
Items have: {id, content, status} where status is one of:
  pending, in_progress, completed, cancelled
"""

from sqlalchemy import String, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class AgentTodo(TimestampedBase):
    __tablename__ = "agent_todos"

    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_app_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # items: [{"id": str, "content": str, "status": "pending"|"in_progress"|"completed"|"cancelled"}]

    def to_dict(self) -> dict:
        return super().to_dict()
