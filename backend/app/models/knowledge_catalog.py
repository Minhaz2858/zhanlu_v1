"""Catalog models for the Project Knowledge Graph — kb_table_meta, kb_column_meta, kb_table_relation.

These are per-KB metadata tables populated by the catalog indexer and queried by
the schema linker at NL2SQL time.  They follow the existing metadata / create_all
convention (no alembic migration required).
"""

from datetime import datetime

from sqlalchemy import String, Text, Integer, Float, DateTime, Boolean, ForeignKey, BigInteger, UniqueConstraint, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class KBTableMeta(TimestampedBase):
    __tablename__ = "kb_table_meta"

    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id"), nullable=False, index=True
    )
    schema_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    table_name: Mapped[str] = mapped_column(String(256), nullable=False)
    table_type: Mapped[str] = mapped_column(String(32), nullable=False, default="TABLE")
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    description_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.utcnow()
    )

    __table_args__ = (
        UniqueConstraint("kb_id", "schema_name", "table_name", name="uq_kb_table_meta"),
    )


class KBColumnMeta(TimestampedBase):
    __tablename__ = "kb_column_meta"

    table_meta_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kb_table_meta.id", ondelete="CASCADE"), nullable=False, index=True
    )
    column_name: Mapped[str] = mapped_column(String(256), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    is_nullable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_primary_key: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    default_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("table_meta_id", "column_name", name="uq_kb_column_meta"),
    )


class KBTableRelation(TimestampedBase):
    __tablename__ = "kb_table_relation"

    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id"), nullable=False, index=True
    )
    source_table_meta_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kb_table_meta.id"), nullable=False, index=True
    )
    target_table_meta_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kb_table_meta.id"), nullable=False, index=True
    )
    relation_type: Mapped[str] = mapped_column(String(16), nullable=False, default="FK")
    source_columns: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    target_columns: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_cardinality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_cardinality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "source_table_meta_id", "target_table_meta_id",
            name="uq_kb_table_relation",
        ),
    )
