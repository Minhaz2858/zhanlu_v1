"""023_agent_memory_embedding

Revision ID: 023
Revises: 022
Create Date: 2026-07-23

Two related changes that landed together:

1. Add a nullable ``embedding`` JSON column to ``agent_memories`` so semantic
   memory recall can store vector embeddings per row. The column is JSON
   (rather than a native ``vector`` type) so it works on both SQLite (used in
   tests) and PostgreSQL (used in production). save_memory() populates it
   lazily; backfill_memory_embeddings.py backfills pre-existing rows in a
   separate one-shot pass.

2. Create the ``swarm_mailbox_messages`` table — the persistent mailbox for
   swarm team members. Schema mirrors the existing
   :class:`app.models.swarm_mailbox.SwarmMailboxMessage` model. The
   ``priority`` column is created here with a default of 0, so the later
   ``024_swarm_mailbox_priority`` migration is a no-op on databases that
   came up through this migration.

Both changes are idempotent: safe to re-run on databases where the column
or table already exist.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "023"
down_revision: Union[str, None] = "022"
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

    # 1. Embedding column on agent_memories.
    if not _has_column(bind, "agent_memories", "embedding"):
        op.add_column(
            "agent_memories",
            sa.Column("embedding", sa.JSON(), nullable=True),
        )

    # 2. swarm_mailbox_messages table (created with priority from the start).
    if not _has_table(bind, "swarm_mailbox_messages"):
        op.create_table(
            "swarm_mailbox_messages",
            # TimestampedBase columns.
            sa.Column(
                "id",
                sa.String(length=36),
                primary_key=True,
            ),
            sa.Column("created_date", sa.DateTime(), nullable=True),
            sa.Column("updated_date", sa.DateTime(), nullable=True),
            sa.Column("created_by_id", sa.String(length=36), nullable=True),
            sa.Column(
                "is_deleted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "org_id",
                sa.String(length=36),
                nullable=False,
                server_default="default-org",
            ),
            sa.Column(
                "app_id",
                sa.String(length=36),
                nullable=False,
                server_default="default-app",
            ),
            # Domain columns.
            sa.Column("team_id", sa.String(length=64), nullable=False),
            sa.Column("sender", sa.String(length=64), nullable=False),
            sa.Column("recipient", sa.String(length=64), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("summary", sa.String(length=500), nullable=True),
            sa.Column(
                "read",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "priority",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        op.create_index(
            "ix_swarm_mailbox_messages_team_id",
            "swarm_mailbox_messages",
            ["team_id"],
        )
        op.create_index(
            "ix_swarm_mailbox_messages_sender",
            "swarm_mailbox_messages",
            ["sender"],
        )
        op.create_index(
            "ix_swarm_mailbox_messages_recipient",
            "swarm_mailbox_messages",
            ["recipient"],
        )
        op.create_index(
            "ix_swarm_mailbox_messages_priority",
            "swarm_mailbox_messages",
            ["priority"],
        )
        op.create_index(
            "ix_swarm_mailbox_messages_org_id",
            "swarm_mailbox_messages",
            ["org_id"],
        )
        op.create_index(
            "ix_swarm_mailbox_messages_app_id",
            "swarm_mailbox_messages",
            ["app_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()

    if _has_column(bind, "agent_memories", "embedding"):
        op.drop_column("agent_memories", "embedding")

    if _has_table(bind, "swarm_mailbox_messages"):
        op.drop_table("swarm_mailbox_messages")