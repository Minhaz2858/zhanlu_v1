"""049_create_project_entity_tables — entity graph + overlay tables.

Revision ID: 049_create_project_entity_tables
Revises: 048_resource_registry
Create Date: 2026-08-10

Creates ``project_entity``, ``project_entity_link`` and
``project_catalog_overlay`` (project-keyed entity graph + human curation
overlays). Idempotent: table-existence checks first.
"""

from alembic import op
import sqlalchemy as sa


revision = "049_create_project_entity_tables"
down_revision = "048_resource_registry"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return set(insp.get_table_names())


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
    ]


def upgrade() -> None:
    existing = _table_names()

    if "project_entity" not in existing:
        op.create_table(
            "project_entity",
            *_base_columns(),
            sa.Column("project_id", sa.String(36), nullable=False),
            sa.Column("name", sa.String(191), nullable=False),
            sa.Column("aliases", sa.JSON(), nullable=True),
            sa.Column("entity_type", sa.String(50), nullable=False, server_default="concept"),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("source", sa.String(50), nullable=False, server_default="memory"),
            sa.Column("source_ref", sa.String(36), nullable=True),
            sa.UniqueConstraint("project_id", "name", name="uq_project_entity"),
        )
        op.create_index("ix_project_entity_project_id", "project_entity", ["project_id"])

    if "project_entity_link" not in existing:
        op.create_table(
            "project_entity_link",
            *_base_columns(),
            sa.Column("entity_id", sa.String(36), nullable=False),
            sa.Column("target_type", sa.String(30), nullable=False),
            sa.Column("target_id", sa.String(191), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("source", sa.String(50), nullable=False, server_default="llm"),
            sa.UniqueConstraint(
                "entity_id", "target_type", "target_id",
                name="uq_project_entity_link",
            ),
        )
        op.create_index(
            "ix_project_entity_link_entity_id", "project_entity_link", ["entity_id"]
        )

    if "project_catalog_overlay" not in existing:
        op.create_table(
            "project_catalog_overlay",
            *_base_columns(),
            sa.Column("project_id", sa.String(36), nullable=False),
            sa.Column("kb_id", sa.String(36), nullable=True),
            sa.Column("table_name", sa.String(191), nullable=True),
            sa.Column("alias", sa.String(191), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("metric_definition", sa.Text(), nullable=True),
            sa.Column("scope", sa.String(20), nullable=False, server_default="table"),
            sa.UniqueConstraint(
                "project_id", "kb_id", "table_name",
                name="uq_project_catalog_overlay",
            ),
        )
        op.create_index(
            "ix_project_catalog_overlay_project_id",
            "project_catalog_overlay",
            ["project_id"],
        )


def downgrade() -> None:
    existing = _table_names()
    for table in ("project_catalog_overlay", "project_entity_link", "project_entity"):
        if table in existing:
            op.drop_table(table)
