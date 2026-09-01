"""Add chat_messages.attachments JSON column.

User-attached files (Kimi/ChatGPT-style history cards) are persisted on the
user's ChatMessage row so the attachment chips survive a page refresh.
The generic entity CRUD round-trips declared model columns, so this column
must exist in the DB for the write/read to carry the data.

Revision ID: 081_chat_messages_attachments
Revises: 080_add_otp_purpose
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "081_chat_messages_attachments"
down_revision = "080_add_otp_purpose"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("attachments", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "attachments")
