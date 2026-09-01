"""022_chat_message_rich_fields

Revision ID: 022
Revises: 021
Create Date: 2026-07-22

Add the ``artifacts`` nullable JSON column to ``chat_messages`` so file
artifact cards survive page refreshes.

NOTE: ``tool_calls`` and ``activity_steps`` already exist on the table
(added out-of-band on existing deployments) and are now declared on the
SQLAlchemy model in the same change set, so the generic entity CRUD
starts round-tripping them too. This migration therefore only adds the
one missing column and is idempotent.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    inspector = sa.inspect(bind)
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "chat_messages", "artifacts"):
        op.add_column("chat_messages", sa.Column("artifacts", sa.JSON(), nullable=True))
    # tool_calls / activity_steps already exist on existing deployments;
    # ensure they are present on any DB where they are somehow missing.
    if not _has_column(bind, "chat_messages", "tool_calls"):
        op.add_column("chat_messages", sa.Column("tool_calls", sa.JSON(), nullable=True))
    if not _has_column(bind, "chat_messages", "activity_steps"):
        op.add_column("chat_messages", sa.Column("activity_steps", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "artifacts")
