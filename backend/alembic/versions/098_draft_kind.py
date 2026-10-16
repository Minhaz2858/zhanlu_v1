"""P3-12: generalize procurement drafts — kind column (po | quote).

Revision ID: 098_draft_kind
Revises: 097_escalations
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa

revision = "098_draft_kind"
down_revision = "097_escalations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "procurement_po_drafts",
        sa.Column("kind", sa.String(20), nullable=False, server_default="po"),
    )
    op.create_index(
        "ix_procurement_po_drafts_kind", "procurement_po_drafts", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_procurement_po_drafts_kind",
                  table_name="procurement_po_drafts")
    op.drop_column("procurement_po_drafts", "kind")
