"""Add agent_runs table for unified agent harness (P1).

Revision ID: 043_agent_runs
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa

revision = "043_agent_runs"
down_revision = "042_ecisco_bi_silent_agent"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_runs",
        # TimestampedBase columns
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # AgentRun columns
        sa.Column("run_id", sa.String(32), unique=True, nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("mode", sa.String(10), nullable=False, server_default="inline"),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("tool_calls", sa.Text(), nullable=True),
        sa.Column("tool_call_count", sa.Integer(), server_default="0"),
        sa.Column("iterations", sa.Integer(), server_default="0"),
        sa.Column("parent_run_id", sa.String(32), nullable=True),
        sa.Column("caller_context", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )

    op.create_index("ix_agent_runs_run_id", "agent_runs", ["run_id"])
    op.create_index("ix_agent_runs_agent_name", "agent_runs", ["agent_name"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_parent_run_id", "agent_runs", ["parent_run_id"])
    op.create_index("ix_agent_runs_org_id", "agent_runs", ["org_id"])
    op.create_index("ix_agent_runs_app_id", "agent_runs", ["app_id"])


def downgrade():
    op.drop_table("agent_runs")
