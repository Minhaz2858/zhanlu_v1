"""Phase 5: Synexia cognitive core — executions, plans, plan_nodes, observation_records.

Revision ID: 007
Revises: 006
Create Date: 2025-07-12
"""

from alembic import op
import sqlalchemy as sa


revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- executions ---
    op.create_table(
        "executions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("agent_name", sa.String(100), nullable=True),
        sa.Column("user_message", sa.Text, nullable=False),
        sa.Column("current_state", sa.String(20), server_default="init", nullable=False),
        sa.Column("mode", sa.String(20), server_default="dynamic", nullable=False),
        sa.Column("task_spec", sa.JSON, nullable=True),
        sa.Column("context_manifest", sa.JSON, nullable=True),
        sa.Column("policy_decision", sa.JSON, nullable=True),
        sa.Column("assistant_content", sa.Text, nullable=True),
        sa.Column("tool_calls", sa.JSON, nullable=True),
        sa.Column("artifact_ids", sa.JSON, nullable=True),
        sa.Column("confidence_score", sa.Float, nullable=True),
        sa.Column("confidence_factors", sa.JSON, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_executions_org_id", "executions", ["org_id"])
    op.create_index("ix_executions_app_id", "executions", ["app_id"])
    op.create_index("ix_executions_conversation_id", "executions", ["conversation_id"])

    # --- plans ---
    op.create_table(
        "plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("execution_id", sa.String(36), sa.ForeignKey("executions.id"), nullable=False),
        sa.Column("version", sa.Integer, server_default="1", nullable=False),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("is_acyclic", sa.Boolean, server_default=sa.text("true"), nullable=False),
    )
    op.create_index("ix_plans_org_id", "plans", ["org_id"])
    op.create_index("ix_plans_app_id", "plans", ["app_id"])
    op.create_index("ix_plans_execution_id", "plans", ["execution_id"])

    # --- plan_nodes ---
    op.create_table(
        "plan_nodes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("node_type", sa.String(30), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("dependencies", sa.JSON, nullable=True),
        sa.Column("inputs", sa.JSON, nullable=True),
        sa.Column("expected_output", sa.String(200), nullable=True),
        sa.Column("output_artifact_type", sa.String(50), nullable=True),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("risk_tier", sa.String(20), nullable=True),
        sa.Column("requires_confirmation", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("result", sa.JSON, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_plan_nodes_org_id", "plan_nodes", ["org_id"])
    op.create_index("ix_plan_nodes_app_id", "plan_nodes", ["app_id"])
    op.create_index("ix_plan_nodes_plan_id", "plan_nodes", ["plan_id"])

    # --- observation_records ---
    op.create_table(
        "observation_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("execution_id", sa.String(36), sa.ForeignKey("executions.id"), nullable=False),
        sa.Column("plan_node_id", sa.String(36), sa.ForeignKey("plan_nodes.id"), nullable=True),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("observation_type", sa.String(30), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=True),
        sa.Column("request_args", sa.JSON, nullable=True),
        sa.Column("result_data", sa.JSON, nullable=True),
        sa.Column("result_text", sa.Text, nullable=True),
        sa.Column("success", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("artifact_ids", sa.JSON, nullable=True),
    )
    op.create_index("ix_observation_records_org_id", "observation_records", ["org_id"])
    op.create_index("ix_observation_records_app_id", "observation_records", ["app_id"])
    op.create_index("ix_observation_records_execution_id", "observation_records", ["execution_id"])


def downgrade() -> None:
    op.drop_table("observation_records")
    op.drop_table("plan_nodes")
    op.drop_table("plans")
    op.drop_table("executions")
