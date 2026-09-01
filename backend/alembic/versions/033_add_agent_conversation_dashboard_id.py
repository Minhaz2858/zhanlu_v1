"""Add ``dashboard_id`` column to ``agent_conversations``.

The chat page now binds each conversation to ONE dashboard (1:1) so that
``update_dashboard`` invocations from the chat can resolve their owning
conversation. The new column mirrors the binding declared on
:class:`~app.models.agent_conversation.AgentConversation`.

Revision ID: 033
Revises: 032
Create Date: 2026-07-30
"""
from alembic import op
import sqlalchemy as sa


revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("agent_conversations")}
    if "dashboard_id" in existing:
        return

    op.add_column(
        "agent_conversations",
        sa.Column("dashboard_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_agent_conversations_dashboard_id",
        "agent_conversations",
        ["dashboard_id"],
        unique=False,
    )
    op.create_foreign_key(
        "agent_conversations_dashboard_id_fkey",
        "agent_conversations",
        "dashboards",
        ["dashboard_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "agent_conversations_dashboard_id_fkey",
        "agent_conversations",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_agent_conversations_dashboard_id",
        table_name="agent_conversations",
    )
    op.drop_column("agent_conversations", "dashboard_id")