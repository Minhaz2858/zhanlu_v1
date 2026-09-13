"""add read column to reports and automation_files

Revision ID: 059_add_read_to_reports_and_automation_files
Revises: 058_user_role_descriptions
Create Date: 2026-08-14

The My Files "unread" badge was only persisted for UserFile. Report and
AutomationFile hardcoded ``read=false`` on the frontend, so the badge never
stayed cleared after a page refresh. This adds the same nullable boolean
``read`` column (Python-side default False) to both tables, mirroring
``user_files.read``.

``automation_executions`` and ``automation_files`` both have models but no
prior migration ever created the tables — they were previously only
materialised via ``Base.metadata.create_all`` at app startup. This migration
creates them EXPLICITLY (mirroring the ORM) so the later ALTER and the rest of
the chain are clean on a fresh database, and is existence-guarded so it is a
no-op on databases where the tables already exist.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "059_add_read_to_reports_and_automation_files"
down_revision = "058_user_role_descriptions"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def _ts_columns() -> list:
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=False, index=True),
    ]


def upgrade():
    if not _table_exists("automation_executions"):
        op.create_table(
            "automation_executions",
            *_ts_columns(),
            sa.Column(
                "automation_task_id",
                sa.String(36),
                sa.ForeignKey("automation_tasks.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("duration_seconds", sa.Integer(), nullable=True),
            sa.Column("output_text", sa.Text(), nullable=True),
            sa.Column("output_data", sa.JSON(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("timeout_at", sa.DateTime(), nullable=True, index=True),
            sa.Column("lease_owner", sa.String(64), nullable=True),
            sa.Column("activity_steps", sa.JSON(), nullable=True),
            sa.Column("current_phase", sa.String(50), nullable=True),
            sa.Column(
                "notified_session_id",
                sa.String(36),
                sa.ForeignKey("chat_sessions.id"),
                nullable=True,
            ),
            sa.Column("parent_execution_id", sa.String(36), nullable=True, index=True),
            sa.Column("email_notified_at", sa.DateTime(), nullable=True),
        )

    if not _table_exists("automation_files"):
        op.create_table(
            "automation_files",
            *_ts_columns(),
            sa.Column(
                "execution_id",
                sa.String(36),
                sa.ForeignKey("automation_executions.id"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "automation_task_id",
                sa.String(36),
                sa.ForeignKey("automation_tasks.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("name", sa.String(500), nullable=False),
            sa.Column("file_type", sa.String(50), nullable=True),
            sa.Column("size", sa.Integer(), nullable=True),
            sa.Column("file_url", sa.String(500), nullable=True),
            sa.Column("file_path", sa.String(1000), nullable=True),
            sa.Column("mime_type", sa.String(100), nullable=True),
            sa.Column("read", sa.Boolean(), nullable=True),
            sa.Column("pinned", sa.Boolean(), nullable=True),
        )

    # Idempotent: the deploy window may have backfilled the column via raw SQL,
    # so `alembic upgrade head` must not crash if it already exists.
    op.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS read boolean")
    op.execute("ALTER TABLE automation_files ADD COLUMN IF NOT EXISTS read boolean")


def downgrade():
    op.execute("ALTER TABLE reports DROP COLUMN IF EXISTS read")
    op.execute("ALTER TABLE automation_files DROP COLUMN IF EXISTS read")
