"""Session-cached data executions table.

Revision ID: 076_data_execution
Revises: 075_chat_messages_live_events
Create Date: 2026-08-24

Adds the ``data_executions`` table backing the session-cached re-export
feature: per-session, per-tool raw result snapshots (JSONB) that later turns
can reuse without re-running expensive queries. Rows carry a ``expires_at``
TTL and the standard TimestampedBase ownership/soft-delete columns.

Idempotent: table existence check first (project convention — avoids tripping
the unapplied migration chain).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "076_data_execution"
down_revision = "075_chat_messages_live_events"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("data_executions"):
        return
    op.create_table(
        "data_executions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column(
            "args",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # TimestampedBase shared columns
        sa.Column(
            "created_date",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_date",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "org_id",
            sa.String(36),
            nullable=False,
            server_default=sa.text("'default-org'"),
        ),
        sa.Column(
            "app_id",
            sa.String(36),
            nullable=False,
            server_default=sa.text("'default-app'"),
        ),
        sa.Index("ix_data_executions_session_id", "session_id"),
        sa.Index("ix_data_executions_tool_name", "tool_name"),
        sa.Index("ix_data_executions_expires_at", "expires_at"),
    )


def downgrade() -> None:
    if _table_exists("data_executions"):
        op.drop_table("data_executions")
