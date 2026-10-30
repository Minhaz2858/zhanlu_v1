"""MetricDefinition — business metric definitions for NL2SQL semantic layer.

Defines a named metric with its base SQL/table/column, aggregation type,
display metadata, and synonyms.  The synonyms are used by the semantic
resolver to map natural language queries to metric references.
"""

from typing import Optional

from sqlalchemy import String, Boolean, Text, JSON, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

METRIC_CATEGORIES = ["revenue", "growth", "engagement", "monetization", "operations", "custom"]
METRIC_AGGREGATIONS = ["sum", "avg", "count", "min", "max", "distinct_count", "custom"]


class MetricDefinition(TimestampedBase):
    """A business metric with SQL definition, synonyms, and display metadata."""

    __tablename__ = "metric_definitions"

    # Identity
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Definition
    datasource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    base_sql: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    base_table: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    base_column: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    aggregation: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    # Semantics
    display_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    format_pattern: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    synonyms: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Governance
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # P5 T1 — authoritative-registry governance fields (additive, migration 111)
    time_column: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    dimensions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    # vocabulary: services/db/ratio_contract.ZERO_DENOMINATOR_BEHAVIORS —
    # reused, never a second vocabulary (P5 invariant I6).
    zero_denominator_behavior: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # structured basis: [{"column", "op", "value"}], op whitelist enforced at CRUD
    filters_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # P1 canonical evidence vocabulary (mirrors OntologyObject.evidence_status)
    evidence_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="verified", server_default="verified")
    # bumped on every mutation (see update_kb_metric) — consumers pin results
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # draft | published | retired — draft/retired are excluded from the
    # enforcement read (query_contract.datasource_metric_bindings)
    lifecycle: Mapped[str] = mapped_column(
        String(20), nullable=False, default="published", server_default="published")
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # ontology object ids this metric backs (P5: ontology metric backing)
    source_object_refs: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
