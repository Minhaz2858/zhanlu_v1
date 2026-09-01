"""050_report_recipes — ReportRecipe table.

Revision ID: 050_report_recipes
Revises: 049_create_project_entity_tables
Create Date: 2026-08-10

Creates ``report_recipes`` (first-class report templates). Idempotent.
"""

from alembic import op
import sqlalchemy as sa


revision = "050_report_recipes"
down_revision = "049_create_project_entity_tables"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return set(insp.get_table_names())


def upgrade() -> None:
    if "report_recipes" in _table_names():
        return
    op.create_table(
        "report_recipes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("required_metrics", sa.JSON(), nullable=True),
        sa.Column("optional_dimensions", sa.JSON(), nullable=True),
        sa.Column("sql_bundle", sa.JSON(), nullable=True),
        sa.Column("charts", sa.JSON(), nullable=True),
        sa.Column("sections", sa.JSON(), nullable=True),
        sa.Column("validation_rules", sa.JSON(), nullable=True),
        sa.Column("output_format", sa.String(20), nullable=False, server_default="markdown"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_report_recipes_project_id", "report_recipes", ["project_id"])
    op.create_index("ix_report_recipes_name", "report_recipes", ["name"])


def downgrade() -> None:
    if "report_recipes" not in _table_names():
        return
    op.drop_table("report_recipes")
