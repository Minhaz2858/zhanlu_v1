"""016_nl2sql_query_log

Revision ID: 016
Revises: 015
Create Date: 2026-07-15

Add ``nl2sql_query_logs`` table for per-request telemetry.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "nl2sql_query_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("binding_id", sa.String(36), nullable=True, index=True),
        sa.Column("agent_app_id", sa.String(36), nullable=True, index=True),
        sa.Column("datasource_id", sa.String(36), nullable=True, index=True),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("generated_sql", sa.Text, nullable=True),
        sa.Column("sql_hash", sa.String(64), nullable=True, index=True),
        sa.Column("validation_errors", sa.JSON, nullable=True),
        sa.Column("policy_decision", sa.String(32), nullable=True),
        sa.Column("row_count", sa.Integer, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("snapshot_id", sa.String(36), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("user_feedback", sa.String(16), nullable=True),
        sa.Column("explanation", sa.Text, nullable=True),
        # TimestampedBase columns
        sa.Column("created_date", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_date", sa.DateTime, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org", index=True),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app", index=True),
    )


def downgrade() -> None:
    op.drop_table("nl2sql_query_logs")
