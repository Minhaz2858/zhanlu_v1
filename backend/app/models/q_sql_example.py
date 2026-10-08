"""Q→SQL training example pairs for few-shot LLM prompting.

Mirrors SQLBot's ``data_training`` table. Each row captures a natural-language
question and the correct SQL answer, scoped to a datasource (and optionally an agent).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class QSqlExample(TimestampedBase):
    """A natural-language → SQL training pair used as few-shot examples in the LLM prompt."""

    __tablename__ = "q_sql_examples"

    question: Mapped[str] = mapped_column(Text, nullable=False)
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    datasource_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    agent_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    embedding_text: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )  # Combined question for the retriever embedder
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Provenance: "manual" (authored in a UI / API) or "workload" (auto-
    # promoted from a VERIFIED successful run — see QSqlExampleService
    # .maybe_auto_promote). Only manual rows may never be overwritten by the
    # auto-promoter.
    source: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, default="manual"
    )
    # sha256 of the normalised SQL — the auto-promoter's dedupe key, so one
    # query does not accumulate one example per phrasing.
    sql_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
