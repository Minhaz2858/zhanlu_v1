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
    coverage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    table_role: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    # entity_master | fact | dimension | bridge | unknown
    entity_master_hints: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # {"entity_type": str, "filter_columns": list[str], "category_column": str,
    #  "sample_categories": list[str]} — discovered filter metadata for
    # entity_master tables; persisted by the LLM at runtime so later queries
    # skip re-discovery.
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


class ProjectEntity(TimestampedBase):
    """A named entity in a project's own vocabulary (fully generic).

    Entity types are universal categories only (``product`` / ``customer`` /
    ``metric`` / ``concept`` / ``organization`` / ``location``); names and
    descriptions always come from the project's own data — never from
    hardcoded domain examples.
    """

    __tablename__ = "project_entity"

    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(191), nullable=False)
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, default="concept")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="memory")
    # memory / chat / file / schema
    source_ref: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True,
        comment="ID of the source memory/conversation that produced this entity (idempotency)",
    )

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_project_entity"),
    )


class ProjectEntityLink(TimestampedBase):
    """Link from a project entity to a catalog/file/memory target."""

    __tablename__ = "project_entity_link"

    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("project_entity.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    target_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # table / column / file / memory / conversation
    target_id: Mapped[str] = mapped_column(String(191), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="llm")
    # llm / embedding / overlay

    __table_args__ = (
        UniqueConstraint(
            "entity_id", "target_type", "target_id",
            name="uq_project_entity_link",
        ),
    )


class ProjectCatalogOverlay(TimestampedBase):
    """Human-curated per-project overrides for catalog entries."""

    __tablename__ = "project_catalog_overlay"

    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kb_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    table_name: Mapped[str | None] = mapped_column(String(191), nullable=True)
    alias: Mapped[str | None] = mapped_column(String(191), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    metric_definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    table_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Per-project override of the auto-classified KBTableMeta.table_role.
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="table")
    # table / kb / table_role

    __table_args__ = (
        UniqueConstraint(
            "project_id", "kb_id", "table_name",
            name="uq_project_catalog_overlay",
        ),
    )


class ProjectMetric(TimestampedBase):
    """A curable per-project business metric (Approach A Business Semantic Layer).

    Metrics are LLM-bootstrapped as ``status='proposed'`` and must be promoted to
    ``status='approved'`` by a human before they are injected into NL2SQL prompts.
    Domain content (SQL patterns, aliases, bindings) lives here as data — never in
    global prompts, per the platform's domain-free convention.
    """

    __tablename__ = "project_metric"

    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kb_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(191), nullable=False)
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql_expression: Mapped[str | None] = mapped_column(Text, nullable=True)
    query_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    default_aggregation: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # sum / avg / max / min / count
    bindings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # [{table, measure_columns, date_column, dimensions}]
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="llm")
    # llm / user
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    # proposed / approved

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_project_metric"),
    )
