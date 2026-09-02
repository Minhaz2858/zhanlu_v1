"""a2a_tasks — external agent delegation records

Revision ID: 085_a2a_tasks
Revises: 084_mcp_servers_columns
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "085_a2a_tasks"
down_revision = "084_mcp_servers_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("app_id", sa.String(36), nullable=False),
        sa.Column("agent_app_id", sa.String(36), nullable=False),
        sa.Column("external", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(20), nullable=False, server_default="submitted"),
        sa.Column("message_parts_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_a2a_tasks_agent_app_id", "a2a_tasks", ["agent_app_id"])
    op.create_index("ix_a2a_tasks_status", "a2a_tasks", ["status"])
    op.create_index("ix_a2a_tasks_org_id", "a2a_tasks", ["org_id"])
    op.create_index("ix_a2a_tasks_app_id", "a2a_tasks", ["app_id"])


def downgrade() -> None:
    op.drop_index("ix_a2a_tasks_app_id", table_name="a2a_tasks")
    op.drop_index("ix_a2a_tasks_org_id", table_name="a2a_tasks")
    op.drop_index("ix_a2a_tasks_status", table_name="a2a_tasks")
    op.drop_index("ix_a2a_tasks_agent_app_id", table_name="a2a_tasks")
    op.drop_table("a2a_tasks")
