"""Business terminology (glossary) for NL2SQL metadata enrichment.

Maps domain-specific words (e.g. "ARR", "churn") to their descriptions so the
LLM can correctly interpret natural-language questions.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import String, Text, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class Terminology(TimestampedBase):
    """A business-glossary entry for NL2SQL prompt enrichment.

    Parent entries can have children (e.g. "Revenue" → "MRR", "ARR", "ARR PU").
    Each entry is scoped to one or more datasources.
    """

    __tablename__ = "terminologies"

    word: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parent_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("terminologies.id"), nullable=True, index=True
    )
    datasource_ids: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )  # JSON list of datasource IDs
    agent_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    embedding_text: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )  # Combined text for the retriever embedder
