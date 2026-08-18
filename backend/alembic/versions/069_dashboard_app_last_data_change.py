"""DashboardApp Phase 2 columns: last_data_change_at, viewed_at.

Revision ID: 069_dashboard_app_last_data_change
Revises: 068_artifact_events
Create Date: 2026-08-18

T4 (Last Updated + unread badge): the realtime poller bumps
``last_data_change_at`` whenever a query hash changes; ``viewed_at`` records
when the user last opened the dashboard. ``unread`` is derived
(``last_data_change_at > viewed_at``) in the router, so no event persistence
is needed here.

Idempotent: raw ``ADD COLUMN IF NOT EXISTS`` (project convention — avoids
tripping the unapplied migration chain).
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "069_dashboard_app_last_data_change"
down_revision = "068_artifact_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE dashboard_apps "
        "ADD COLUMN IF NOT EXISTS last_data_change_at timestamp with time zone"
    )
    op.execute(
        "ALTER TABLE dashboard_apps "
        "ADD COLUMN IF NOT EXISTS viewed_at timestamp with time zone"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE dashboard_apps DROP COLUMN IF EXISTS viewed_at")
    op.execute("ALTER TABLE dashboard_apps DROP COLUMN IF EXISTS last_data_change_at")
