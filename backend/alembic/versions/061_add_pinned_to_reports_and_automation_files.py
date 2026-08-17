"""add pinned column to reports and automation_files

Revision ID: 061_add_pinned_to_reports_and_automation_files
Revises: 060_resource_access_policies
Create Date: 2026-08-14

The My Files "Pin" action was a no-op for Report and AutomationFile: the
frontend hardcoded ``pinned=false`` for those two types and ``togglePin``
early-returned, so only UserFile (which already had a ``pinned`` column)
could be pinned. This adds the same nullable boolean ``pinned`` column
(Python-side default False) to both tables, mirroring ``user_files.pinned``.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "061_add_pinned_to_reports_and_automation_files"
down_revision = "060_resource_access_policies"
branch_labels = None
depends_on = None


def upgrade():
    # Idempotent: the deploy window may have backfilled the column via raw SQL,
    # so `alembic upgrade head` must not crash if it already exists.
    op.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS pinned boolean")
    op.execute("ALTER TABLE automation_files ADD COLUMN IF NOT EXISTS pinned boolean")


def downgrade():
    op.execute("ALTER TABLE reports DROP COLUMN IF EXISTS pinned")
    op.execute("ALTER TABLE automation_files DROP COLUMN IF EXISTS pinned")
