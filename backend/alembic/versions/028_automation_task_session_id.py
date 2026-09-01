"""028_automation_task_session_id

Add a nullable ``session_id`` ForeignKey to ``automation_tasks`` so the
sidebar can mark the chat session that originated an automation with a
small clock icon (Option A: lightweight visual indicator).

The column is nullable because:

  * Automations created outside the chat flow (e.g. from the
    ``/automation`` page) have no origin session and should still work.
  * Existing rows created before this migration don't have a session
    to backfill, so a NOT NULL constraint would require either a
    best-guess backfill or rejecting the migration on legacy data.

Revision ID: 028
Revises: 027
Create Date: 2026-07-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "028"
down_revision: Union[str, None] = "027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("automation_tasks") as batch_op:
        batch_op.add_column(sa.Column("session_id", sa.String(36), nullable=True))
        batch_op.create_index("ix_automation_tasks_session_id", ["session_id"])
        # FK constraint with ON DELETE SET NULL: if a user deletes the
        # originating chat session, the automation should stay
        # (otherwise deleting an old chat would silently destroy the
        # user's scheduled task). The session_id is just an annotation
        # for the sidebar — when the session goes away we null the FK
        # so the automation is no longer linked to a stale session.
        batch_op.create_foreign_key(
            "fk_automation_tasks_session",
            "chat_sessions",
            ["session_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("automation_tasks") as batch_op:
        batch_op.drop_constraint("fk_automation_tasks_session", type_="foreignkey")
        batch_op.drop_index("ix_automation_tasks_session_id")
        batch_op.drop_column("session_id")
