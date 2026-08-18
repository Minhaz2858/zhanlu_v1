"""Artifact usage instrumentation table.

Revision ID: 068_artifact_events
Revises: 067_dashboard_apps
Create Date: 2026-08-18

Adds ``artifact_events`` — an append-only log of deck lifecycle events
(generated / edited / downloaded).  Mirrors ``TimestampedBase`` columns so it
joins cleanly with the rest of the schema.  Idempotent per project convention.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "068_artifact_events"
down_revision = "067_dashboard_apps"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("artifact_events"):
        return
    op.create_table(
        "artifact_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("artifact_id", sa.String(36), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        # TimestampedBase shared columns
        sa.Column(
            "created_date",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_date",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "org_id",
            sa.String(36),
            nullable=False,
            server_default=sa.text("'default-org'"),
        ),
        sa.Column(
            "app_id",
            sa.String(36),
            nullable=False,
            server_default=sa.text("'default-app'"),
        ),
        sa.Index("ix_artifact_events_artifact_id", "artifact_id"),
        sa.Index("ix_artifact_events_event_type", "event_type"),
        sa.Index("ix_artifact_events_user_id", "user_id"),
        sa.Index(
            "ix_artifact_events_type_created",
            "event_type",
            "created_date",
        ),
    )


def downgrade() -> None:
    if _table_exists("artifact_events"):
        op.drop_table("artifact_events")
