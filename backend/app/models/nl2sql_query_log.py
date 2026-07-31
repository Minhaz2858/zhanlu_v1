"""Per-request telemetry for NL2SQL queries."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import String, Text, Integer, Float, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class Nl2sqlQueryLog(TimestampedBase):
    """Immutable telemetry row written for every ``nl2sql.ask()`` invocation."""

    __tablename__ = "nl2sql_query_logs"

    binding_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    agent_app_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    datasource_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    generated_sql: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sql_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    validation_errors: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    policy_decision: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    snapshot_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # success / denied / error
    user_feedback: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # thumbs_up / thumbs_down
    explanation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
