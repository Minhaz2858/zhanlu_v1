"""add read column to reports and automation_files

Revision ID: 059_add_read_to_reports_and_automation_files
Revises: 058_user_role_descriptions
Create Date: 2026-08-14

The My Files "unread" badge was only persisted for UserFile. Report and
AutomationFile hardcoded ``read=false`` on the frontend, so the badge never
stayed cleared after a page refresh. This adds the same nullable boolean
``read`` column (Python-side default False) to both tables, mirroring
``user_files.read``.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "059_add_read_to_reports_and_automation_files"
down_revision = "058_user_role_descriptions"
branch_labels = None
depends_on = None


def upgrade():
    # Idempotent: the deploy window may have backfilled the column via raw SQL,
    # so `alembic upgrade head` must not crash if it already exists.
    op.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS read boolean")
    op.execute("ALTER TABLE automation_files ADD COLUMN IF NOT EXISTS read boolean")


def downgrade():
    op.execute("ALTER TABLE reports DROP COLUMN IF EXISTS read")
    op.execute("ALTER TABLE automation_files DROP COLUMN IF EXISTS read")
