"""Email Notification Gateway settings on automation tasks/executions.

Revision ID: 071_email_notification_settings
Revises: 070_dashboard_app_phase2_columns
Create Date: 2026-08-20

Adds the per-task email notification columns (``notify_emails`` JSON array,
``notify_on`` enum, ``attach_file`` flag) to ``automation_tasks`` and the
``email_notified_at`` observability stamp to ``automation_executions``.

Idempotent: raw ``ADD COLUMN IF NOT EXISTS`` (project convention — avoids
tripping the unapplied migration chain).
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "071_email_notification_settings"
down_revision = "070_dashboard_app_phase2_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE automation_tasks "
        "ADD COLUMN IF NOT EXISTS notify_enabled boolean"
    )
    op.execute(
        "ALTER TABLE automation_tasks "
        "ADD COLUMN IF NOT EXISTS notify_emails json"
    )
    op.execute(
        "ALTER TABLE automation_tasks "
        "ADD COLUMN IF NOT EXISTS notify_on varchar(20)"
    )
    op.execute(
        "ALTER TABLE automation_tasks "
        "ADD COLUMN IF NOT EXISTS attach_file boolean"
    )
    op.execute(
        "ALTER TABLE automation_executions "
        "ADD COLUMN IF NOT EXISTS email_notified_at timestamp"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE automation_executions DROP COLUMN IF EXISTS email_notified_at"
    )
    op.execute(
        "ALTER TABLE automation_tasks DROP COLUMN IF EXISTS attach_file"
    )
    op.execute(
        "ALTER TABLE automation_tasks DROP COLUMN IF EXISTS notify_on"
    )
    op.execute(
        "ALTER TABLE automation_tasks DROP COLUMN IF EXISTS notify_emails"
    )
    op.execute(
        "ALTER TABLE automation_tasks DROP COLUMN IF EXISTS notify_enabled"
    )
