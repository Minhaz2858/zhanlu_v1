"""SemanticMapping — maps database columns to semantic display names and synonyms.

Each mapping links a (table, column) pair in a datasource to a user-friendly
display name, data type hint, and a list of synonyms.  Relationship hints
(join keys, PK/FK, dimension/measure) guide join generation.
"""

from typing import Optional

from sqlalchemy import String, Boolean, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class SemanticMapping(TimestampedBase):
    """Semantic layer — maps raw (table, column) to human-readable metadata."""

    __tablename__ = "semantic_mappings"

    # References
    datasource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    metric_definition_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Mapping target
    table_name: Mapped[str] = mapped_column(String(200), nullable=False)
    column_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Semantics
    display_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    data_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    synonyms: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Relationship hints
    join_key_to: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    is_primary_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_foreign_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_dimension: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_measure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
