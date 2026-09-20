"""KnowledgeBase model — database connection and file source fields."""

from datetime import datetime

from sqlalchemy import String, Text, Integer, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class KnowledgeBase(TimestampedBase):
    __tablename__ = "knowledge_bases"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    project: Mapped[str | None] = mapped_column(String(255), nullable=True, default="global")
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=True, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Type and source
    type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Database connection fields
    db_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    database_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    schema: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # File source fields
    file_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Stats
    item_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="active")

    # Document indexing state (source_kind='file' only)
    # Lifecycle: None -> "pending" -> "indexing" -> "ready" | "failed"
    indexing_status: Mapped[str | None] = mapped_column(
        String(50), nullable=True, default=None
    )
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    index_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_indexed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # Semantic catalog state (any connector-backed dialect — the gate is
    # knowledge_graph.catalog_triggers.supports_catalog, derived from the
    # connector registry, NOT a hardcoded dialect list)
    # Lifecycle: "pending" -> "indexing" -> "ready" | "error"
    # Populated by knowledge_graph.catalog_indexer when SEMANTIC_CATALOG_ENABLED=True.
    catalog_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )

    # ── NL2SQL source lifecycle (db/readiness.py owns the vocabulary) ──────
    # connected -> discovering -> indexed -> enriched -> validated -> nl2sql_ready
    # failure branches: discovery_failed | enrichment_partial | validation_failed
    # "" means "never staged on this build" — the gate then derives an honest
    # state from catalog_status instead of assuming readiness. A source that is
    # not nl2sql_ready is refused or disclosed, never queried silently.
    nl2sql_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="", index=True
    )
    # Why the source is in that state — shown to the operator, never invented.
    nl2sql_state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    nl2sql_state_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
