"""Business Semantic Layer — coverage_json on kb_table_meta + project_metric table.

Revision ID: 064_business_semantic_layer
Revises: 063_add_weekly_digest_summaries
Create Date: 2026-08-15

Adds:
- ``kb_table_meta.coverage_json`` (JSON) — per-table data coverage metadata
  ``{date_column, min_date, max_date, probed_at}`` probed at index time.
- ``project_metric`` table — curable per-project business metric registry with
  a human approval gate (status proposed|approved).

Idempotent: column/table existence checks first.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "064_business_semantic_layer"
down_revision = "063_add_weekly_digest_summaries"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns(table)}
    return column in existing


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


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
    if not _column_exists("kb_table_meta", "coverage_json"):
        op.add_column(
            "kb_table_meta",
            sa.Column("coverage_json", sa.JSON(), nullable=True),
        )

    if not _table_exists("project_metric"):
        op.create_table(
            "project_metric",
            *_base_columns(),
            sa.Column("project_id", sa.String(36), nullable=False),
            sa.Column("kb_id", sa.String(36), nullable=True),
            sa.Column("name", sa.String(191), nullable=False),
            sa.Column("aliases", sa.JSON(), nullable=True),
            sa.Column("definition", sa.Text(), nullable=True),
            sa.Column("sql_expression", sa.Text(), nullable=True),
            sa.Column("query_pattern", sa.Text(), nullable=True),
            sa.Column("unit", sa.String(32), nullable=True),
            sa.Column("default_aggregation", sa.String(16), nullable=True),
            sa.Column("bindings", sa.JSON(), nullable=True),
            sa.Column("source", sa.String(16), nullable=False, server_default="llm"),
            sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
            sa.UniqueConstraint("project_id", "name", name="uq_project_metric"),
        )
        op.create_index("ix_project_metric_project_id", "project_metric", ["project_id"])
        op.create_index("ix_project_metric_kb_id", "project_metric", ["kb_id"])


def downgrade() -> None:
    if _table_exists("project_metric"):
        op.drop_table("project_metric")
    if _column_exists("kb_table_meta", "coverage_json"):
        op.drop_column("kb_table_meta", "coverage_json")
