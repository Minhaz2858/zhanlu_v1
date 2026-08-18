"""DashboardApp Phase 2 columns: chat_thread_id, scope.

Revision ID: 070_dashboard_app_phase2_columns
Revises: 069_dashboard_app_last_data_change
Create Date: 2026-08-18

T5 (chat thread <-> artifact linking): ``chat_thread_id`` stores the
agent_conversations.id that created the app — powers My Files "Open in chat".
T10 (personal/company scope): ``scope`` gates visibility (default "personal").

Idempotent: raw ``ADD COLUMN IF NOT EXISTS`` (project convention — avoids
tripping the unapplied migration chain).
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "070_dashboard_app_phase2_columns"
down_revision = "069_dashboard_app_last_data_change"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE dashboard_apps "
        "ADD COLUMN IF NOT EXISTS chat_thread_id varchar(36)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dashboard_apps_chat_thread_id "
        "ON dashboard_apps (chat_thread_id)"
    )
    op.execute(
        "ALTER TABLE dashboard_apps "
        "ADD COLUMN IF NOT EXISTS scope varchar(20) DEFAULT 'personal' NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE dashboard_apps DROP COLUMN IF EXISTS scope"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_dashboard_apps_chat_thread_id"
    )
    op.execute(
        "ALTER TABLE dashboard_apps DROP COLUMN IF EXISTS chat_thread_id"
    )
