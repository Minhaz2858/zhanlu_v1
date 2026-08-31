"""024_swarm_mailbox_priority

Revision ID: 024
Revises: 023
Create Date: 2026-07-23

Add the ``priority`` NOT NULL INTEGER column (default 0) to
``swarm_mailbox_messages`` so the mailbox is a priority queue rather than
strict FIFO. Existing rows default to priority 0 (normal), preserving the
historical timestamp ordering among them.

Idempotent: safe to re-run.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, table: str) -> bool:
    inspector = sa.inspect(bind)
    return inspector.has_table(table)


def _has_column(bind, table: str, column: str) -> bool:
    if not _has_table(bind, table):
        return False
    inspector = sa.inspect(bind)
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "swarm_mailbox_messages", "priority"):
        op.add_column(
            "swarm_mailbox_messages",
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "swarm_mailbox_messages", "priority"):
        op.drop_column("swarm_mailbox_messages", "priority")
