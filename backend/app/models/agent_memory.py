"""AgentMemory model — persistent agent memory across conversations.

Stores agent notes (target="memory") and user profile data (target="user").
Extended with SHA-256 content hashing for dedup, TTL expiration,
importance scoring, and usage tracking.
"""

from sqlalchemy import String, Text, Integer, DateTime, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class AgentMemory(TimestampedBase):
    __tablename__ = "agent_memories"

    agent_app_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Project scope (2026-08-05). When set, the memory row is recalled
    # only when the active conversation's project_id matches this
    # value. NULL = legacy cross-project (no project at write time)
    # or the explicit "user profile" rows (target='user' is always
    # cross-project — it's about WHO the user is, not what they did
    # in a specific project). The Q2 2026 sales report leak that
    # surfaced when users opened convs in different projects was
    # because this column didn't exist; legacy rows therefore always
    # matched the by-user filter and got injected into every
    # conversation's system prompt. Going forward, the memory tool
    # stamps the active project's id at write time so a note taken
    # in "Data Analysis" stays inside that project.
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    target: Mapped[str] = mapped_column(String(20), nullable=False)  # "memory" or "user"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Advanced memory fields (P2 enhancement)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Pinned memory (2026-08-27): pinned entries are always injected first
    # and survive compaction/eviction — the "always-in-context" layer.
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Semantic recall (P0 — agent-improvement roadmap). Stores the content's
    # embedding vector as a JSON float list so it works on both SQLite and
    # Postgres without requiring a pgvector extension. Populated best-effort
    # by save_memory()/backfill_embeddings(); when absent search_memories()
    # falls back to lexical token-overlap scoring.
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> dict:
        result = super().to_dict()
        return result
