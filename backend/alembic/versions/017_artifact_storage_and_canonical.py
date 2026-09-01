"""017_artifact_storage_and_canonical

Revision ID: 017
Revises: 016
Create Date: 2026-07-15

Add ``storage_uri`` column to ``artifact_blobs``, make ``data`` nullable,
and add ``canonical_format`` column to ``artifacts``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add canonical_format to artifacts
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.add_column(
            sa.Column("canonical_format", sa.String(10), nullable=True)
        )

    # Add storage_uri and make data nullable in artifact_blobs
    with op.batch_alter_table("artifact_blobs") as batch_op:
        batch_op.add_column(
            sa.Column("storage_uri", sa.String(500), nullable=True)
        )
        batch_op.alter_column(
            "data", existing_type=sa.LargeBinary, nullable=True
        )

    # Backfill storage_uri for existing blobs stored inline
    op.execute(
        "UPDATE artifact_blobs "
        "SET storage_uri = 'inline://' || id "
        "WHERE data IS NOT NULL AND storage_uri IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("artifact_blobs") as batch_op:
        batch_op.drop_column("storage_uri")
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_column("canonical_format")
    # Note: cannot revert data column to non-nullable in downgrade
