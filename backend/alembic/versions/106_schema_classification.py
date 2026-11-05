"""Stage 2B-1: dynamic schema classification columns (P0 audit follow-up).

Chained after 105_identity_resolution (a separate in-flight migration on the
same uncommitted batch) to avoid two alembic heads on 104_ontology_revisions.

Revision ID: 106_schema_classification
Revises: 105_identity_resolution
Create Date: 2026-10-30

Additive only — no behavior change, no backfill in the migration itself
(scripts/backfill_schema_classification.py does the inference as a separate,
reviewable step). Column naming note: the new scope columns are prefixed
dataset_ because project_catalog_overlay already carries a `scope` column
with unrelated semantics (table/kb/table_role).
"""

from alembic import op
import sqlalchemy as sa

revision = "106_schema_classification"
down_revision = "105_identity_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: the live DB may already carry some columns from a
    # partially-applied run (startup migration crashed mid-way once).
    insp = sa.inspect(op.get_bind())
    kb_cols = {c["name"] for c in insp.get_columns("kb_table_meta")}
    pco_cols = {c["name"] for c in insp.get_columns("project_catalog_overlay")}

    for col, typ in [
        ("schema_class", sa.String(16)),
        ("schema_class_confidence", sa.Float()),
        ("schema_class_signals_json", sa.JSON()),
        ("dataset_scope", sa.String(64)),
        ("dataset_scope_confidence", sa.Float()),
        ("dataset_scope_signals_json", sa.JSON()),
    ]:
        if col not in kb_cols:
            op.add_column("kb_table_meta", sa.Column(col, typ, nullable=True))

    for col, typ in [
        ("schema_class_override", sa.String(16)),
        ("dataset_scope_override", sa.String(64)),
    ]:
        if col not in pco_cols:
            op.add_column("project_catalog_overlay", sa.Column(col, typ, nullable=True))


def downgrade() -> None:
    op.drop_column("project_catalog_overlay", "dataset_scope_override")
    op.drop_column("project_catalog_overlay", "schema_class_override")
    op.drop_column("kb_table_meta", "dataset_scope_signals_json")
    op.drop_column("kb_table_meta", "dataset_scope_confidence")
    op.drop_column("kb_table_meta", "dataset_scope")
    op.drop_column("kb_table_meta", "schema_class_signals_json")
    op.drop_column("kb_table_meta", "schema_class_confidence")
    op.drop_column("kb_table_meta", "schema_class")
