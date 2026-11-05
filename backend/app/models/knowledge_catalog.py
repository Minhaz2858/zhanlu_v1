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
    # ── source-truth columns (2026-09-20) ──────────────────────────────────
    # The database's OWN words and structure, read from information_schema /
    # pg_catalog at index time. NEVER written by the description LLM, so an
    # operator can always tell a customer's label apart from our inference:
    # ``source_comment`` is verbatim, and ``description_source`` says which of
    # the two a given description came from ('llm' | 'source_comment' | '').
    source_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full index inventory as the source declares it:
    # [{"name","columns":[...],"unique":bool,"primary":bool}]. An empty list
    # means "the source declares no index on this object", None means "not
    # collected" — those are different facts and must not be conflated.
    indexes_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Which kind of count row_count carries: 'exact' (COUNT(*), bounded by
    # ROW_COUNT_TIMEOUT_S) or 'estimated' (engine statistics, e.g.
    # pg_class.reltuples / information_schema TABLE_ROWS). NULL = unknown.
    row_count_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 'llm' | 'source_comment' | '' (never described).
    description_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="", server_default=""
    )
    # OCI #1 (2026-09-22): how much to trust the descriptions. 0.0 blank,
    # 0.3 unknown-marker, 0.6 plain LLM, 0.8 data-corroborated, 0.9 source
    # comment, 1.0 human-verified (set by the catalog PATCH endpoints).
    # Recomputed on every index EXCEPT human rows (source='human').
    description_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    coverage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Denormalised business-domain label written by domain_discovery; lets the
    # TOC renderer and prompt builder group tables without a join.
    domain_name: Mapped[str | None] = mapped_column(String(191), nullable=True, index=True)
    table_role: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    # entity_master | fact | dimension | bridge | aggregate | reporting_view
    # | forecast | archive | unknown
    # P-SV: "one row = what" — auto-inferred at index time (PK / FK set /
    # date column), human-overridable via project_catalog_overlay.grain.
    # This is the metadata the Source Verification Gate checks BEFORE
    # SQL generation (e.g. reject `orders` when the question needs
    # `order_items` grain).
    grain: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Human-set source-authority override for this table:
    # 'authoritative' | 'standard' | 'weak'. NULL = compute from signals
    # (approved metric bindings > fact/entity role > stale/archive).
    authority_override: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # ── Stage 2B-1 dynamic schema classification (2026-10-30) ──────────────
    # system | business | unknown — how the classifier sees the table.
    # Populated by the indexer/backfill; NEVER auto-promoted beyond its
    # confidence; a human overlay override (schema_class_override) always
    # wins. Named dataset_* to avoid colliding with the overlay's existing
    # `scope` column (which means table/kb/table_role).
    schema_class: Mapped[str | None] = mapped_column(String(16), nullable=True)
    schema_class_confidence: Mapped[float | None] = mapped_column(nullable=True)
    schema_class_signals_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # global | partition:<key_name> | NULL (undetermined). The partition
    # VALUE stays in dataset_scope_signals_json — the column stores only the
    # partition key name, so a value rename never rewrites classifications.
    dataset_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dataset_scope_confidence: Mapped[float | None] = mapped_column(nullable=True)
    dataset_scope_signals_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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
    # The source's own column comment, verbatim (MySQL COLUMN_COMMENT,
    # PostgreSQL col_description). Most ERPs label their columns here and
    # nowhere else — this is the customer's own semantic, not our inference.
    source_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Index role as the source declares it, normalised across dialects:
    # 'PRI' (primary key) | 'UNI' (unique) | 'MUL' (non-unique index) | '' |
    # None ("not collected"). The full inventory lives on KBTableMeta.indexes_json.
    key_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 'llm' | 'source_comment' | 'human' | '' (never described).
    description_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="", server_default=""
    )
    # See KBTableMeta.description_confidence — same scale, per column.
    description_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Measured distribution from a bounded row sample (2026-09-20, slice 2) —
    # see services/knowledge_graph/column_stats.py.  Keys: rows_sampled, basis
    # ('full' | 'sample'), nulls, null_rate, distinct, distinct_is_exact,
    # top_values [{value, count}], and type-dependent min/max/avg/zeros or
    # min_len/max_len.  An ABSENT key means "not measured" — never 0, never [].
    # ``none_as_null=True`` is load-bearing, not cosmetic: a plain ``JSON``
    # column stores a Python ``None`` as the JSON LITERAL ``null``, so
    # ``stats_json IS NULL`` is false for a column that was never measured —
    # which is exactly the "absent key, never a zero" distinction this whole
    # slice depends on. With it, "not measured" is a real SQL NULL.
    stats_json: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    # ── semantic typing (data-driven, dialect- and domain-agnostic) ──
    # identifier | measure | dimension | temporal | flag | free_text | unknown
    semantic_type: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    # Distinct values seen for low-cardinality columns — a status/type/venue
    # dictionary the model can filter on without guessing literals.
    enum_values: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Unit read off the data by the description LLM (元, 吨, %, USD, ...).
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)

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


class KBDomain(TimestampedBase):
    """A discovered business domain inside one KnowledgeBase's catalog.

    Produced by ``services/knowledge_graph/domain_discovery.py`` from structural
    (Louvain over FK/join edges) and semantic (K-Means over table embeddings)
    partitions merged by centroid cosine similarity. ``name_en`` / ``name_zh``
    are LLM-proposed from the member tables' own names, descriptions and sample
    values — nothing about a domain is hardcoded in the platform.
    """

    __tablename__ = "kb_domain"

    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id"), nullable=False, index=True
    )
    name_en: Mapped[str] = mapped_column(String(191), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(191), nullable=True)
    slug: Mapped[str | None] = mapped_column(String(191), nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # llm | derived — whether the name came from the naming model or the
    # deterministic token fallback.
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="derived")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.utcnow()
    )

    __table_args__ = (
        UniqueConstraint("kb_id", "name_en", name="uq_kb_domain"),
    )


class KBDomainMember(TimestampedBase):
    """Membership of one catalog table in a discovered business domain."""

    __tablename__ = "kb_domain_member"

    domain_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kb_domain.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    kb_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    table_meta_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kb_table_meta.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    table_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint("domain_id", "table_meta_id", name="uq_kb_domain_member"),
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
    # P-SV: human grain declaration — "one row = what" (beats auto-inference).
    grain: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="table")
    # table / kb / table_role
    # ── Stage 2B-1 (2026-10-30): human overrides for the schema classifier.
    # A human verdict ALWAYS beats the auto-inferred kb_table_meta columns —
    # inferred classifications must never become unquestioned facts.
    # schema_class_override: system | business | unknown
    # dataset_scope_override: global | partition:<key_name> | unknown
    schema_class_override: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dataset_scope_override: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
