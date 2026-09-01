"""Restore the ``project`` string column on 5 tables.

Migration 020 converted the loose ``project`` string column into a
``project_id`` ForeignKey and then dropped the old string column.
The ORM models, however, still declare ``project`` (String(255),
nullable) and the seed / application code writes to it
(e.g. ``project='global'``), so the column must exist. This migration
re-adds it so the schema matches the models.

Tables affected (same set as migration 020):
    agent_apps, automation_tasks, chat_sessions, user_files,
    knowledge_bases

Revision ID: 032
Revises: 031
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


_TABLES = (
    "agent_apps",
    "automation_tasks",
    "chat_sessions",
    "user_files",
    "knowledge_bases",
)


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("project", sa.String(255), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("project")
