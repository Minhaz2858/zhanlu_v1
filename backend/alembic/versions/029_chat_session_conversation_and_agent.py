"""029_chat_session_conversation_and_agent

Revision ID: 029
Revises: 028
Create Date: 2026-07-24

Persist the link from a ``ChatSession`` to its ``AgentConversation`` (and
the agent name the session was opened with) so that reopening a session
can:

  1. Resume the same ``AgentConversation`` id — avoid orphan convs and
     keep the Recent Chats list from growing one row per user message.
  2. Re-attach the same agent without prompting the user.

Background
----------
The frontend (``Chat.jsx``) has long tried to persist this link by
calling::

    base44.entities.ChatSession.update(sid, {
        conversation_id: convId,
        agent_name: activeAgent.name,
    })

but the generic entity service in ``app/services/entity_service.py``
filters updates to the model's declared columns, so when the
``ChatSession`` model didn't declare these two columns the payload was
silently dropped — the HTTP request returned 200, the frontend assumed
the write succeeded, and on the next reload both fields were ``None``.
This migration (and the matching model update) closes the loop so the
link is actually persisted.

Both columns are nullable:

  * Legacy ``ChatSession`` rows created before this migration have no
    linked conv / agent, and they must keep working — opening an old
    session just falls through to "create new conv" the way it does
    today.
  * New-system-agent ChatSessions (created without a user-selected
    agent) may never set ``agent_name``; that's fine.

The ``conversation_id`` FK uses ``ON DELETE SET NULL`` because
deleting an AgentConversation should NOT cascade-delete the user's
chat history. The session just forgets which conv it owned and the
next send creates a new one — exactly the existing behaviour when
the link was lost.

Idempotent in live mode (safe to re-run on a DB where the columns
already exist); degrades to unconditional SQL in offline ``--sql``
mode (so ``alembic upgrade --sql`` and ``alembic downgrade --sql``
both produce a complete script). Modeled on
``025_agent_conversation_project_id.py`` which fixed the same class
of bug for ``AgentConversation.project_id``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    """Live-mode column-existence check. Always False in offline mode."""
    if op.get_context().as_sql:
        return False
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return result is not None


def _constraint_exists(conn, table: str, constraint: str) -> bool:
    """Live-mode FK-existence check. Always False in offline mode."""
    if op.get_context().as_sql:
        return False
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_name = :t AND constraint_name = :c"
        ),
        {"t": table, "c": constraint},
    ).fetchone()
    return result is not None


def _index_exists(conn, table: str, index: str) -> bool:
    """Live-mode index-existence check. Always False in offline mode."""
    if op.get_context().as_sql:
        return False
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM pg_indexes WHERE tablename = :t AND indexname = :i"
        ),
        {"t": table, "i": index},
    ).fetchone()
    return result is not None


def upgrade() -> None:
    conn = op.get_bind()

    # ── chat_sessions.conversation_id ────────────────────────────────
    if not _column_exists(conn, "chat_sessions", "conversation_id"):
        with op.batch_alter_table("chat_sessions") as batch_op:
            batch_op.add_column(
                sa.Column("conversation_id", sa.String(36), nullable=True)
            )
    if not _index_exists(
        conn, "chat_sessions", "ix_chat_sessions_conversation_id"
    ):
        with op.batch_alter_table("chat_sessions") as batch_op:
            batch_op.create_index(
                "ix_chat_sessions_conversation_id", ["conversation_id"]
            )
    if not _constraint_exists(
        conn, "chat_sessions", "fk_chat_sessions_conversation"
    ):
        with op.batch_alter_table("chat_sessions") as batch_op:
            batch_op.create_foreign_key(
                "fk_chat_sessions_conversation",
                "agent_conversations",
                ["conversation_id"],
                ["id"],
                ondelete="SET NULL",
            )

    # ── chat_sessions.agent_name ────────────────────────────────────
    if not _column_exists(conn, "chat_sessions", "agent_name"):
        with op.batch_alter_table("chat_sessions") as batch_op:
            batch_op.add_column(
                sa.Column("agent_name", sa.String(255), nullable=True)
            )


def downgrade() -> None:
    """Drop everything 029 added. We intentionally DO NOT gate on
    ``_column_exists`` / ``_index_exists`` / ``_constraint_exists`` here
    — in offline ``--sql`` mode those return ``False`` and the drops
    would be skipped, producing an empty script. In live mode alembic's
    normal "this revision has already been applied" guard catches
    double-runs, and a partial-state downgrade is exactly the case
    where you'd want to surface the error rather than silently skip
    half the drops."""
    with op.batch_alter_table("chat_sessions") as batch_op:
        batch_op.drop_constraint(
            "fk_chat_sessions_conversation", type_="foreignkey"
        )
        batch_op.drop_index("ix_chat_sessions_conversation_id")
        batch_op.drop_column("conversation_id")
        batch_op.drop_column("agent_name")