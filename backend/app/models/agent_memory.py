"""AgentMemory model — persistent agent memory across conversations.

Stores agent notes (target="memory") and user profile data (target="user").
Extended with SHA-256 content hashing for dedup, TTL expiration,
importance scoring, and usage tracking.
"""

from sqlalchemy import String, Text, Integer, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class AgentMemory(TimestampedBase):
    __tablename__ = "agent_memories"

    agent_app_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    target: Mapped[str] = mapped_column(String(20), nullable=False)  # "memory" or "user"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Advanced memory fields (P2 enhancement)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Semantic recall (P0 — agent-improvement roadmap). Stores the content's
    # embedding vector as a JSON float list so it works on both SQLite and
    # Postgres without requiring a pgvector extension. Populated best-effort
    # by save_memory()/backfill_embeddings(); when absent search_memories()
    # falls back to lexical token-overlap scoring.
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> dict:
        result = super().to_dict()
        return result
