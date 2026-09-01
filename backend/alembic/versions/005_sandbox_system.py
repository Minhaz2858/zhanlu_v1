"""Phase 3: Sandbox system — sandbox_jobs, sandbox_job_events, sandbox_commands.

Revision ID: 005
Revises: 004
Create Date: 2025-07-12
"""

from alembic import op
import sqlalchemy as sa


revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- sandbox_jobs ---
    op.create_table(
        "sandbox_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("artifact_id", sa.String(36), nullable=True),
        sa.Column("artifact_version_id", sa.String(36), nullable=True),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("skill_name", sa.String(100), nullable=False),
        sa.Column("skill_version", sa.String(50), nullable=True),
        sa.Column("input_package", sa.JSON, nullable=True),
        sa.Column("output_spec", sa.JSON, nullable=True),
        sa.Column("status", sa.String(20), server_default="queued", nullable=False),
        sa.Column("container_id", sa.String(100), nullable=True),
        sa.Column("image_name", sa.String(200), nullable=True),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("timeout_seconds", sa.Integer, server_default="120", nullable=False),
        sa.Column("output_artifact_ids", sa.JSON, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("exit_code", sa.Integer, nullable=True),
        sa.Column("memory_used_mb", sa.Integer, nullable=True),
        sa.Column("cpu_time_seconds", sa.Integer, nullable=True),
    )
    op.create_index("ix_sandbox_jobs_org_id", "sandbox_jobs", ["org_id"])
    op.create_index("ix_sandbox_jobs_app_id", "sandbox_jobs", ["app_id"])
    op.create_index("ix_sandbox_jobs_artifact_id", "sandbox_jobs", ["artifact_id"])
    op.create_index("ix_sandbox_jobs_conversation_id", "sandbox_jobs", ["conversation_id"])
    op.create_index("ix_sandbox_jobs_execution_id", "sandbox_jobs", ["execution_id"])

    # --- sandbox_job_events ---
    op.create_table(
        "sandbox_job_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("sandbox_jobs.id"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("data_json", sa.JSON, nullable=True),
        sa.Column("timestamp", sa.DateTime, nullable=True),
    )
    op.create_index("ix_sandbox_job_events_org_id", "sandbox_job_events", ["org_id"])
    op.create_index("ix_sandbox_job_events_app_id", "sandbox_job_events", ["app_id"])
    op.create_index("ix_sandbox_job_events_job_id", "sandbox_job_events", ["job_id"])

    # --- sandbox_commands ---
    op.create_table(
        "sandbox_commands",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("sandbox_jobs.id"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("command", sa.Text, nullable=False),
        sa.Column("cwd", sa.String(500), nullable=True),
        sa.Column("exit_code", sa.Integer, nullable=True),
        sa.Column("stdout", sa.Text, nullable=True),
        sa.Column("stderr", sa.Text, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_sandbox_commands_org_id", "sandbox_commands", ["org_id"])
    op.create_index("ix_sandbox_commands_app_id", "sandbox_commands", ["app_id"])
    op.create_index("ix_sandbox_commands_job_id", "sandbox_commands", ["job_id"])


def downgrade() -> None:
    op.drop_table("sandbox_commands")
    op.drop_table("sandbox_job_events")
    op.drop_table("sandbox_jobs")
