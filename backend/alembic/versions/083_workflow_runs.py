"""workflow_runs + workflow_run_steps

Revision ID: 083_workflow_runs
Revises: 082_chat_shares
Create Date: 2026-09-01
"""
from alembic import op
import sqlalchemy as sa

revision = "083_workflow_runs"
down_revision = "082_chat_shares"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        sa.Column("agent_app_id", sa.String(36), nullable=False, index=True),
        sa.Column("trigger", sa.String(50), nullable=False, server_default="chat"),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "workflow_run_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        sa.Column("run_id", sa.String(36), nullable=False, index=True),
        sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column("node_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("workflow_run_steps")
    op.drop_table("workflow_runs")
