"""Governance models — policy decisions, approvals, cost ledger, and audit logs.

These four tables form the governance and platform services layer:
- PolicyDecision: every allow/deny/require_confirm decision recorded
- ApprovalRequest: user approval workflow for high-risk actions
- CostLedger: token, sandbox, and storage cost tracking per execution
- AuditLog: append-only audit trail for compliance
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, Integer, Float, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


# Approval statuses
APPROVAL_STATUSES = ["pending", "approved", "rejected", "expired", "cancelled"]

# Cost types
COST_TYPES = ["llm_tokens", "sandbox_job", "artifact_build", "storage", "api_call"]


class PolicyDecision(TimestampedBase):
    """A policy decision recorded for audit — every allow/deny/require_confirm.

    Created by the PolicyEvaluator at both GATE (whole-plan) and ACT (per-node)
    levels.  Provides a complete audit trail of why each decision was made.
    """

    __tablename__ = "policy_decisions"

    execution_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    plan_node_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    decision_level: Mapped[str] = mapped_column(String(20), nullable=False)  # gate | node
    decision: Mapped[str] = mapped_column(String(20), nullable=False)  # allow | deny | require_confirm
    risk_tier: Mapped[str] = mapped_column(String(20), default="low", nullable=False)
    reasons: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    conditions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    policy_packs: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class ApprovalRequest(TimestampedBase):
    """A user approval request for high-risk actions.

    When the PolicyEvaluator returns "require_confirm", an ApprovalRequest
    is created.  Execution pauses until the user approves or rejects it.
    """

    __tablename__ = "approval_requests"

    execution_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    plan_node_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    artifact_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Request details
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)  # publish_artifact | sandbox_exec | external_send | code_skill
    action_description: Mapped[str] = mapped_column(Text, nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)

    # Status
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)

    # Reviewer
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Expiration
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Context
    context_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class CostLedger(TimestampedBase):
    """Cost tracking record — every LLM call, sandbox job, and artifact build.

    Records the cost of each operation for budget enforcement and billing.
    """

    __tablename__ = "cost_ledger"

    execution_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    agent_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Cost details
    cost_type: Mapped[str] = mapped_column(String(30), nullable=False)  # llm_tokens | sandbox_job | artifact_build | storage | api_call
    cost_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cost_currency: Mapped[str] = mapped_column(String(10), default="USD", nullable=False)

    # LLM-specific
    model_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Sandbox-specific
    sandbox_job_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    sandbox_duration_seconds: Mapped[Optional[float]] = mapped_column(nullable=True)

    # Metadata
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class AuditLog(TimestampedBase):
    """Append-only audit log — every significant action recorded for compliance.

    Never updated or deleted.  Provides a complete trail of who did what,
    when, and why.
    """

    __tablename__ = "audit_logs"

    # Actor
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)  # user | agent | system
    actor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    actor_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Action
    action: Mapped[str] = mapped_column(String(50), nullable=False)  # create | update | delete | execute | approve | reject | publish
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)  # agent | skill | artifact | execution | policy
    resource_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Context
    org_id: Mapped[str] = mapped_column(String(36), default="default-org", nullable=False)
    app_id: Mapped[str] = mapped_column(String(36), default="default-app", nullable=False)
    execution_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Details
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    details_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Result
    result: Mapped[str] = mapped_column(String(20), default="success", nullable=False)  # success | failure | denied
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timing
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
