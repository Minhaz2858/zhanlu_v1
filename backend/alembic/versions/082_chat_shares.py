"""Add chat_shares table (Kimi/GPT-style conversation sharing).

Owners create a token per chat session; the read-only public page
``/share/c/<token>`` renders the conversation without auth.

Revision ID: 082_chat_shares
Revises: 081_chat_messages_attachments
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "082_chat_shares"
down_revision = "081_chat_messages_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_shares",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
    )
    op.create_index("ix_chat_shares_session_id", "chat_shares", ["session_id"])
    op.create_index("ix_chat_shares_token", "chat_shares", ["token"])


def downgrade() -> None:
    op.drop_index("ix_chat_shares_token", table_name="chat_shares")
    op.drop_index("ix_chat_shares_session_id", table_name="chat_shares")
    op.drop_table("chat_shares")
