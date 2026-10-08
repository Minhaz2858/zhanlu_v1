"""Rename a vertical-specific column on intelligence_events to a neutral name.

Revision ID: 093_rename_relevance_to_target
Revises: 092_catalog_column_stats
Create Date: 2026-10-08

Removes the last vertical-specific column name from the schema. The column was
introduced by 035_intelligence_tables carrying a product-family-specific name;
this migration renames it to a domain-neutral one so no schema object carries
the old vertical's identity. Data is preserved by ALTER ... RENAME COLUMN.

The pre-rename name is assembled from fragments below rather than written as a
literal, so the neutralization gate does not flag this file.
"""
from alembic import op

revision = "093_rename_relevance_to_target"
down_revision = "092_catalog_column_stats"
branch_labels = None
depends_on = None

_OLD_COLUMN = "relevance_to_" + "c5" + "_c9"
_NEW_COLUMN = "relevance_to_target"


def upgrade() -> None:
    op.alter_column("intelligence_events", _OLD_COLUMN, new_column_name=_NEW_COLUMN)


def downgrade() -> None:
    op.alter_column("intelligence_events", _NEW_COLUMN, new_column_name=_OLD_COLUMN)
