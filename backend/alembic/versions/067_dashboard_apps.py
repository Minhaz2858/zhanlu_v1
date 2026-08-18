"""Full-Stack Dashboard App table.

Revision ID: 067_dashboard_apps
Revises: 066_entity_master_table_role
Create Date: 2026-08-18

Adds the ``dashboard_apps`` table for the new full-stack dashboard pipeline:
- one row per deployed dashboard app (FastAPI sub-router + pre-built React dist)
- ``spec`` JSONB holds the full ``DashboardSpec``
- ``status`` lifecycle: building → running → stopped → error

Idempotent: table existence check first (project convention — avoids tripping
the unapplied 057-061 migration chain).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "067_dashboard_apps"
down_revision = "066_entity_master_table_role"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("dashboard_apps"):
        return
    op.create_table(
        "dashboard_apps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("datasource_kb_id", sa.String(120), nullable=False),
        sa.Column("design_system_ref", sa.String(255), nullable=True),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'building'"),
        ),
        sa.Column("app_url", sa.String(255), nullable=True),
        sa.Column(
            "refresh_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
        ),
        sa.Column("project_id", sa.String(36), nullable=True),
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
        sa.UniqueConstraint("slug", name="uq_dashboard_apps_slug"),
        sa.UniqueConstraint("org_id", "slug", name="ix_dashboard_apps_org_slug"),
        sa.Index("ix_dashboard_apps_slug", "slug"),
    )


def downgrade() -> None:
    if _table_exists("dashboard_apps"):
        op.drop_table("dashboard_apps")
