"""Phase 7: Governance system — policy_decisions, approval_requests, cost_ledger, audit_logs.

Revision ID: 009
Revises: 008
Create Date: 2025-07-12
"""

from alembic import op
import sqlalchemy as sa


revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- policy_decisions ---
    op.create_table(
        "policy_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("plan_node_id", sa.String(36), nullable=True),
        sa.Column("decision_level", sa.String(20), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("risk_tier", sa.String(20), server_default="low", nullable=False),
        sa.Column("reasons", sa.JSON, nullable=True),
        sa.Column("conditions", sa.JSON, nullable=True),
        sa.Column("policy_packs", sa.JSON, nullable=True),
        sa.Column("evaluated_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_policy_decisions_org_id", "policy_decisions", ["org_id"])
    op.create_index("ix_policy_decisions_app_id", "policy_decisions", ["app_id"])
    op.create_index("ix_policy_decisions_execution_id", "policy_decisions", ["execution_id"])

    # --- approval_requests ---
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("plan_node_id", sa.String(36), nullable=True),
        sa.Column("artifact_id", sa.String(36), nullable=True),
        sa.Column("action_type", sa.String(50), nullable=False),
        sa.Column("action_description", sa.Text, nullable=False),
        sa.Column("risk_tier", sa.String(20), server_default="medium", nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("reviewed_by", sa.String(36), nullable=True),
        sa.Column("reviewed_at", sa.DateTime, nullable=True),
        sa.Column("review_notes", sa.Text, nullable=True),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("context_json", sa.JSON, nullable=True),
    )
    op.create_index("ix_approval_requests_org_id", "approval_requests", ["org_id"])
    op.create_index("ix_approval_requests_app_id", "approval_requests", ["app_id"])
    op.create_index("ix_approval_requests_execution_id", "approval_requests", ["execution_id"])

    # --- cost_ledger ---
    op.create_table(
        "cost_ledger",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("agent_name", sa.String(100), nullable=True),
        sa.Column("cost_type", sa.String(30), nullable=False),
        sa.Column("cost_amount", sa.Float, server_default="0", nullable=False),
        sa.Column("cost_currency", sa.String(10), server_default="USD", nullable=False),
        sa.Column("model_name", sa.String(100), nullable=True),
        sa.Column("prompt_tokens", sa.Integer, nullable=True),
        sa.Column("completion_tokens", sa.Integer, nullable=True),
        sa.Column("total_tokens", sa.Integer, nullable=True),
        sa.Column("sandbox_job_id", sa.String(36), nullable=True),
        sa.Column("sandbox_duration_seconds", sa.Float, nullable=True),
        sa.Column("metadata_json", sa.JSON, nullable=True),
        sa.Column("recorded_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_cost_ledger_org_id", "cost_ledger", ["org_id"])
    op.create_index("ix_cost_ledger_app_id", "cost_ledger", ["app_id"])
    op.create_index("ix_cost_ledger_execution_id", "cost_ledger", ["execution_id"])

    # --- audit_logs ---
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("actor_name", sa.String(100), nullable=True),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column("resource_id", sa.String(36), nullable=True),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("details_json", sa.JSON, nullable=True),
        sa.Column("result", sa.String(20), server_default="success", nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("occurred_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_audit_logs_org_id", "audit_logs", ["org_id"])
    op.create_index("ix_audit_logs_app_id", "audit_logs", ["app_id"])
    op.create_index("ix_audit_logs_execution_id", "audit_logs", ["execution_id"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("cost_ledger")
    op.drop_table("approval_requests")
    op.drop_table("policy_decisions")
