"""AuditEvent — structured audit events complementing the plain-text audit_logs table.

Tracks specific event types (nl2sql_query, datasource_connect, metric_access,
policy_violation, etc.) with structured detail, actor tracing, policy
decisions, and row/cost metrics for NL2SQL calls.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Index, String, Integer, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

# ── Phase 7C (C2): registry ↔ reality reconciliation ────────────────────────
# The registry documents the event vocabulary; it is NOT a write-time
# validator (historical/legacy events must stay readable, and the dynamic
# review family below is intentionally open). The contract test
# (tests/test_audit_event_registry.py) asserts every statically-known
# writer type is registered here.
#
# Dynamic families (documented, not enumerable):
#   * review decisions:  f"{resource_type}_{decision}" — the shared
#     `_record_review` convention (e.g. ontology_object_confirmed,
#     ontology_link_rejected, agent_app_created...)
#   * business rule lifecycle: f"business_rule_{decision}" where decision
#     ∈ {confirmed, rejected, enabled, disabled, resubmitted}
AUDIT_EVENT_TYPES = [
    "nl2sql_query", "datasource_connect", "metric_access",
    "policy_violation", "skill_create", "skill_publish",
    "agent_config", "template_upload", "learning_apply",
    "system_config",
    # Ontology (written by services/ontology/{fanout,escalation}.py and
    # permissions.py since P0-2; registered 2026-10-30 — gap audit G3-bug)
    "ontology_fanout", "escalation_created", "escalation_resolved",
    # Enterprise resources + lifecycle (Phase 0B/4/7C)
    "resource_deleted", "resource_onboarded", "outcome_verified",
    # Identity resolution (P3)
    "identity_review",
    # Document evidence review (Phase 3B)
    "ontology_document_review",
    # Business rules (Phase 1B/7C)
    "business_rule_check", "business_rule_guidelines_synced",
    "business_rule_confirmed", "business_rule_rejected",
    "business_rule_enabled", "business_rule_disabled",
    "business_rule_resubmitted",
    # ERP submission outcomes (erp_tools)
    "erp_submission_succeeded", "erp_submission_failed",
    # External MCP transport security events (Phase 8.2)
    "mcp_auth_success", "mcp_auth_failed", "mcp_tool_invoked",
    # Automation control plane (written by automation/control_plane.py +
    # routers/automation_api.py — reconciled 2026-11-03, Phase 9)
    "automation_confirmed", "automation_run_forced",
]
AUDIT_EVENT_SOURCES = [
    "nl2sql", "studio", "agent", "governance", "system", "user",
    "ontology",
    # written by enterprise/{cleanup,onboarding,business_rules}.py and
    # identity review since Phase 0B — reconciled 2026-11-02 (7C)
    "enterprise",
    # external MCP transport auth/tool events (Phase 8.2)
    "mcp",
    # automation control plane (Phase 9 reconciliation)
    "automation",
]
# "completed" (onboarding runs), "error" (outcome_verified failures) and
# "positive" (escalation CSAT) are written in production — registered.
AUDIT_OUTCOMES = [
    "success", "failure", "denied", "warning",
    "completed", "error", "positive",
]


class AuditEvent(TimestampedBase):
    """Structured audit event with NL2SQL-specific fields and policy tracking."""

    __tablename__ = "audit_events"
    # Phase 11 (S3): index parity with migration 014.
    __table_args__ = (
        Index("ix_audit_events_event_type", "event_type"),
        Index("ix_audit_events_occurred_at", "occurred_at"),
        Index("ix_audit_events_trace_id", "trace_id"),
    )

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
