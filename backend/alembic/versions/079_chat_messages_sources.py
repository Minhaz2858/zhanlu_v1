"""Add sources JSON column to chat_messages for data-source citations.

Revision ID: 079_chat_messages_sources
Revises: 078_agent_invocation_observability
Create Date: 2026-08-31
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "079_chat_messages_sources"
down_revision = "078_agent_invocation_observability"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chat_messages",
        sa.Column("sources", sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column("chat_messages", "sources")
