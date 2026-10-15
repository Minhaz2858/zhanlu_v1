"""Procurement PO drafts — P0-4 first write-back (2026-10-15).

One org-scoped table; RLS applied inline at creation (born empty).
Revises: 094_ontology_core
"""

import sqlalchemy as sa
from alembic import op

revision = "095_procurement_po_drafts"
down_revision = "094_ontology_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "procurement_po_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=True),
        sa.Column("supplier_name", sa.String(255), nullable=False),
        sa.Column("supplier_code", sa.String(64), nullable=True),
        sa.Column("product", sa.String(255), nullable=True),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("est_unit_price", sa.Float(), nullable=True),
        sa.Column("est_amount", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(16), nullable=True),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="approved"),
        sa.Column("approval_id", sa.String(36), nullable=True),
        sa.Column("extras_json", sa.JSON(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
    )
    op.execute("ALTER TABLE procurement_po_drafts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE procurement_po_drafts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON procurement_po_drafts USING ("
        "  coalesce(current_setting('app.current_org', true), '') = ''"
        "  OR org_id = current_setting('app.current_org', true)"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON procurement_po_drafts")
    op.drop_table("procurement_po_drafts")
