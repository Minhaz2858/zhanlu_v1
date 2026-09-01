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

    # Semantic catalog state (db_type in (mysql, postgres) only)
    # Lifecycle: "pending" -> "indexing" -> "ready" | "error"
    # Populated by knowledge_graph.catalog_indexer when SEMANTIC_CATALOG_ENABLED=True.
    catalog_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
