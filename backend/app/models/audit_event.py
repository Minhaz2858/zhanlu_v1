"""AuditEvent — structured audit events complementing the plain-text audit_logs table.

Tracks specific event types (nl2sql_query, datasource_connect, metric_access,
policy_violation, etc.) with structured detail, actor tracing, policy
decisions, and row/cost metrics for NL2SQL calls.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

AUDIT_EVENT_TYPES = [
    "nl2sql_query", "datasource_connect", "metric_access",
    "policy_violation", "skill_create", "skill_publish",
    "agent_config", "template_upload", "learning_apply",
    "system_config",
]
AUDIT_EVENT_SOURCES = ["nl2sql", "studio", "agent", "governance", "system", "user"]
AUDIT_OUTCOMES = ["success", "failure", "denied", "warning"]


class AuditEvent(TimestampedBase):
    """Structured audit event with NL2SQL-specific fields and policy tracking."""

    __tablename__ = "audit_events"

    # Event identity
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    event_source: Mapped[str] = mapped_column(String(50), nullable=False)
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Actor
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    actor_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Target
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # NL2SQL-specific
    datasource_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    binding_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    sql_text_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    query_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Policy
    policy_decision: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    policy_reasons: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Outcome
    outcome: Mapped[str] = mapped_column(String(20), default="success", nullable=False)
    detail_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timing
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
