"""048_resource_registry — Unified Resource Registry table.

Revision ID: 048_resource_registry
Revises: 047_knowledge_bases_catalog_status
Create Date: 2026-08-10

Creates ``resource_registry`` (one row per indexed project resource with
visibility tiers). Idempotent: table-existence check first, so re-running
is a no-op (the table may already exist if created by metadata.create_all).
"""

from alembic import op
import sqlalchemy as sa


revision = "048_resource_registry"
down_revision = "047_knowledge_bases_catalog_status"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return set(insp.get_table_names())


def upgrade() -> None:
    if "resource_registry" in _table_names():
        return
    op.create_table(
        "resource_registry",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("resource_type", sa.String(30), nullable=False),
        sa.Column("resource_id", sa.String(191), nullable=True),
        sa.Column("name", sa.String(256), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("entities", sa.JSON(), nullable=True),
        sa.Column("owner_user_id", sa.String(36), nullable=True),
        sa.Column("visibility", sa.String(20), nullable=False, server_default="project"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("last_indexed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "project_id", "resource_type", "resource_id",
            name="uq_resource_registry",
        ),
    )
    op.create_index("ix_resource_registry_project_id", "resource_registry", ["project_id"])
    op.create_index("ix_resource_registry_org_id", "resource_registry", ["org_id"])
    op.create_index("ix_resource_registry_app_id", "resource_registry", ["app_id"])


def downgrade() -> None:
    if "resource_registry" not in _table_names():
        return
    op.drop_table("resource_registry")
