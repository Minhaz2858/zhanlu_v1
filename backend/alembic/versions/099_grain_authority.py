"""P-SV: grain metadata + authority — the Source Verification Gate foundation.

Revision ID: 099_grain_authority
Revises: 098_draft_kind
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa

revision = "099_grain_authority"
down_revision = "098_draft_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kb_table_meta",
                  sa.Column("grain", sa.String(200), nullable=True))
    op.add_column("kb_table_meta",
                  sa.Column("authority_override", sa.String(20), nullable=True))
    op.add_column("project_catalog_overlay",
                  sa.Column("grain", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("project_catalog_overlay", "grain")
    op.drop_column("kb_table_meta", "authority_override")
    op.drop_column("kb_table_meta", "grain")
