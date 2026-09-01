"""Add live_events JSON column to chat_messages for typed activity stream.

Revision ID: 075_chat_messages_live_events
Revises: 074_challenger_shadow_runs
Create Date: 2026-08-22
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "075_chat_messages_live_events"
down_revision = "074_challenger_shadow_runs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chat_messages",
        sa.Column("live_events", sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column("chat_messages", "live_events")
