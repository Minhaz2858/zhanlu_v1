"""KnowledgeBase model — database connection and file source fields."""

from datetime import datetime

from sqlalchemy import String, Text, Integer, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase
from app.services.crypto import decrypt_value, encrypt_value


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
    # 2026-10-21 CREDENTIAL-AT-REST (audit E1): the underlying column is
    # `password`, but access goes through the `password` property below, which
    # transparently decrypts on read and encrypts (fail-closed) on write, so
    # plaintext never reaches the database or the API response.
    _password: Mapped[str | None] = mapped_column("password", String(255), nullable=True)
    api_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    @property
    def password(self) -> str | None:
        """Decrypted credential for in-memory use (connections, tests)."""
        return decrypt_value(self._password) if self._password else None

    @password.setter
    def password(self, value: str | None) -> None:
        """Encrypt on write. Fail-closed: a crypto failure refuses the write
        instead of silently persisting plaintext (fixes the crypto fail-open
        gap). Already-encrypted values pass through unchanged."""
        if not value or value.startswith("enc:v1:"):
            self._password = value
            return
        enc = encrypt_value(value)
        if not enc or not enc.startswith("enc:v1:"):
            raise RuntimeError(
                "Refusing to persist KnowledgeBase password: encryption failed"
            )
        self._password = enc

    # File source fields
    file_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # ── P4 T1: document identity (one file KB = one document) ─────────────
    # Current-version POINTER only — the historical rows live in the
    # immutable `document_versions` table (D5: version rows are never
    # updated or deleted). NULL for legacy rows not yet re-indexed.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("document_versions.id"), nullable=True
    )

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


class DocumentVersion(TimestampedBase):
    """P4 T1 / D5 — IMMUTABLE document version record.

    One row per distinct content state of a file KB's document. Rows are
    NEVER updated or deleted: "current" is a pointer on
    ``KnowledgeBase.content_version_id``, and "superseded" is a property of
    proposals that reference an older version — never of version rows.
    """

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("org_id", "kb_id", "content_hash",
                         name="uq_document_version_hash"),
    )

    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id"), nullable=False, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_url_at_ingest: Mapped[str | None] = mapped_column(String(500), nullable=True)
    first_indexed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False
    )
