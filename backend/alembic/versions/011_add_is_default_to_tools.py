"""Add is_default flag to tools table for the default-skill system.

Revision ID: 011
Revises: 010
Create Date: 2025-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add nullable boolean column — existing rows get NULL (treated as False)
    op.add_column(
        "tools",
        sa.Column("is_default", sa.Boolean(), nullable=True),
    )
    # Index for fast lookups of default skills
    op.create_index(
        "ix_tools_is_default",
        "tools",
        ["is_default"],
    )


def downgrade() -> None:
    op.drop_index("ix_tools_is_default", table_name="tools")
    op.drop_column("tools", "is_default")
