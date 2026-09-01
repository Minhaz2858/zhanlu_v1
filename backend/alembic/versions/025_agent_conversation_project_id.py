"""025_agent_conversation_project_id

Revision ID: 025
Revises: 024
Create Date: 2026-07-24

Add the ``project_id`` ForeignKey column to ``agent_conversations`` so
project-scoped agent conversations can be tagged and the data-source
runtime can inherit the project's KnowledgeBases.  The Python model
(``AgentConversation.project_id``) already declares this column, but no
migration ever created it — every INSERT/SELECT touching the column
raised ``UndefinedColumn`` → 500 on ``POST /agents/conversations``,
which is the root cause of "agent not responding" (the conversation
can't even be created).

Also adds the ``phase`` JSON column to ``chat_messages`` so the
Claude-style phase headline (Fathoming / Fabricating / …) survives a
page refresh.  The frontend already sends ``phase`` on
``ChatMessage.update``; the generic entity service silently drops
unknown columns, so it was never persisted.

Idempotent: safe to re-run.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    """Check if a column already exists (idempotency guard)."""
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return result is not None


def _constraint_exists(conn, table: str, constraint: str) -> bool:
    """Check if a foreign-key constraint already exists."""
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_name = :t AND constraint_name = :c"
        ),
        {"t": table, "c": constraint},
    ).fetchone()
    return result is not None


def _index_exists(conn, table: str, index: str) -> bool:
    """Check if an index already exists."""
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM pg_indexes WHERE tablename = :t AND indexname = :i"
        ),
        {"t": table, "i": index},
    ).fetchone()
    return result is not None


def upgrade() -> None:
    conn = op.get_bind()

    # ── agent_conversations.project_id ──────────────────────────────
    if not _column_exists(conn, "agent_conversations", "project_id"):
        with op.batch_alter_table("agent_conversations") as batch_op:
            batch_op.add_column(
                sa.Column("project_id", sa.String(36), nullable=True)
            )
    if not _index_exists(conn, "agent_conversations", "ix_agent_conversations_project_id"):
        with op.batch_alter_table("agent_conversations") as batch_op:
            batch_op.create_index("ix_agent_conversations_project_id", ["project_id"])
    if not _constraint_exists(conn, "agent_conversations", "fk_agent_conversations_project"):
        with op.batch_alter_table("agent_conversations") as batch_op:
            batch_op.create_foreign_key(
                "fk_agent_conversations_project",
                "projects",
                ["project_id"],
                ["id"],
            )

    # ── chat_messages.phase ─────────────────────────────────────────
    if not _column_exists(conn, "chat_messages", "phase"):
        with op.batch_alter_table("chat_messages") as batch_op:
            batch_op.add_column(sa.Column("phase", sa.JSON, nullable=True))


def downgrade() -> None:
    conn = op.get_bind()

    if _constraint_exists(conn, "agent_conversations", "fk_agent_conversations_project"):
        with op.batch_alter_table("agent_conversations") as batch_op:
            batch_op.drop_constraint("fk_agent_conversations_project", type_="foreignkey")
    if _index_exists(conn, "agent_conversations", "ix_agent_conversations_project_id"):
        with op.batch_alter_table("agent_conversations") as batch_op:
            batch_op.drop_index("ix_agent_conversations_project_id")
    if _column_exists(conn, "agent_conversations", "project_id"):
        with op.batch_alter_table("agent_conversations") as batch_op:
            batch_op.drop_column("project_id")

    if _column_exists(conn, "chat_messages", "phase"):
        with op.batch_alter_table("chat_messages") as batch_op:
            batch_op.drop_column("phase")
