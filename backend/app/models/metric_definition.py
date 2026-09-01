"""MetricDefinition — business metric definitions for NL2SQL semantic layer.

Defines a named metric with its base SQL/table/column, aggregation type,
display metadata, and synonyms.  The synonyms are used by the semantic
resolver to map natural language queries to metric references.
"""

from typing import Optional

from sqlalchemy import String, Boolean, Text, JSON
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
