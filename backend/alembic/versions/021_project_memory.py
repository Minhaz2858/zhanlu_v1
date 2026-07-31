"""021_project_memory

Revision ID: 021
Revises: 020
Create Date: 2026-07-20

Add the ``project_memories`` table for project-scoped shared memory.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("agent_app_id", sa.String(36), sa.ForeignKey("agent_apps.id"), nullable=True),
        sa.Column("entry_type", sa.String(50), nullable=False, server_default="fact"),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True, index=True),
        sa.Column("importance", sa.Integer, nullable=True, server_default="0"),
        sa.Column("ttl_days", sa.Integer, nullable=True),
        sa.Column("usage_count", sa.Integer, nullable=True, server_default="0"),
        sa.Column("source_conversation_id", sa.String(36), nullable=True),
        sa.Column("source_artifact_id", sa.String(36), nullable=True),
        sa.Column("last_accessed_at", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("org_id", sa.String(36), nullable=True, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=True, server_default="default-app"),
        sa.Column("is_deleted", sa.Integer, nullable=True, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("project_memories")
