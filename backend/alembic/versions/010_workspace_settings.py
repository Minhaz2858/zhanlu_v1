"""Alembic migration: workspace_settings table for org-level flags.

The first user is the ``auto_bind_all_datasources`` opt-in flag (per
DATA-CORE-3). The table is generic — future workspace-level toggles
reuse it without schema changes.
"""

from alembic import op
import sqlalchemy as sa


revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_settings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), server_default="default-org", nullable=False),
        sa.Column("app_id", sa.String(36), server_default="default-app", nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.Text, nullable=False, server_default=""),
    )
    # Lookups by (org, app, key) are the hot path.
    op.create_index(
        "ix_workspace_settings_lookup",
        "workspace_settings",
        ["org_id", "app_id", "key"],
    )
    op.create_index(
        "ix_workspace_settings_org_id",
        "workspace_settings",
        ["org_id"],
    )
    op.create_index(
        "ix_workspace_settings_app_id",
        "workspace_settings",
        ["app_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_settings_app_id", table_name="workspace_settings")
    op.drop_index("ix_workspace_settings_org_id", table_name="workspace_settings")
    op.drop_index("ix_workspace_settings_lookup", table_name="workspace_settings")
    op.drop_table("workspace_settings")
