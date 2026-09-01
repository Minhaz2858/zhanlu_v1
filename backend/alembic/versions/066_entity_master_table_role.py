"""Entity Master Filter — table_role + entity_master_hints on kb_table_meta.

Revision ID: 066_entity_master_table_role
Revises: 065_smart_alerts
Create Date: 2026-08-18

Adds:
- ``kb_table_meta.table_role`` (VARCHAR(32), default 'unknown') — structural
  role classification: entity_master | fact | dimension | bridge | unknown.
- ``kb_table_meta.entity_master_hints`` (JSON, nullable) — discovered filter
  metadata for entity_master tables
  ({"entity_type", "filter_columns", "category_column", "sample_categories"}).
- ``project_catalog_overlay.table_role`` (VARCHAR(32), nullable) — per-project
  override of the auto-classified table role.

Idempotent: column existence checks first (project convention).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "066_entity_master_table_role"
down_revision = "065_smart_alerts"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns(table)}
    return column in existing


def upgrade() -> None:
    # ── kb_table_meta: table_role + entity_master_hints ──────────────────
    if not _column_exists("kb_table_meta", "table_role"):
        op.add_column(
            "kb_table_meta",
            sa.Column(
                "table_role",
                sa.String(32),
                nullable=False,
                server_default=sa.text("'unknown'"),
            ),
        )
    if not _column_exists("kb_table_meta", "entity_master_hints"):
        op.add_column(
            "kb_table_meta",
            sa.Column("entity_master_hints", sa.JSON(), nullable=True),
        )

    # ── project_catalog_overlay: per-project table_role override ────────
    if not _column_exists("project_catalog_overlay", "table_role"):
        op.add_column(
            "project_catalog_overlay",
            sa.Column("table_role", sa.String(32), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("project_catalog_overlay", "table_role"):
        op.drop_column("project_catalog_overlay", "table_role")
    if _column_exists("kb_table_meta", "entity_master_hints"):
        op.drop_column("kb_table_meta", "entity_master_hints")
    if _column_exists("kb_table_meta", "table_role"):
        op.drop_column("kb_table_meta", "table_role")
