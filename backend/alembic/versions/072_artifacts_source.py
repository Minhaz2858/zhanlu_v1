"""Add ``source`` to artifacts for dashboard-turn artifact guarding (T18).

Revision ID: 072_artifacts_source
Revises: 071_email_notification_settings
Create Date: 2026-08-21

When a turn is a dashboard-intent turn, the artifact persistence layer drops
any artifact whose ``source != 'dashboard_app'`` so a stray analytics-path
artifact (e.g. a static "Web page" written from the narration sentence) cannot
land on the same thread as the real dashboard app.  The ``source`` column makes
that distinction durable and queryable.

Idempotent: raw ``ADD COLUMN IF NOT EXISTS`` + index (project convention).
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "072_artifacts_source"
# Chain off the actual applied DB head (the project has a divergent migration
# history; 057_add_llm_model_capability_fields is the live head in zhanlu-backend).
down_revision = "057_add_llm_model_capability_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE artifacts "
        "ADD COLUMN IF NOT EXISTS source varchar(50) NOT NULL DEFAULT 'agent'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_artifacts_source "
        "ON artifacts (source)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_artifacts_source")
    op.execute("ALTER TABLE artifacts DROP COLUMN IF EXISTS source")
