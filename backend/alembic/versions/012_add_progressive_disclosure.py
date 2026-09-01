"""Add progressive_disclosure columns for Phase 0 Claude-style skill loading.

Revision ID: 012
Revises: 011
Create Date: 2025-07-15

- tools.summary: short description (≤500 chars) for prompt injection
- tools.tags_progressive: JSON array of tags for filtering
- agent_apps.progressive_disclosure: Boolean flag — True for new agents,
  False for existing rows (backward compat)
"""

from alembic import op
import sqlalchemy as sa


revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add columns to tools table (nullable — no default needed)
    op.add_column(
        "tools",
        sa.Column("summary", sa.String(500), nullable=True),
    )
    op.add_column(
        "tools",
        sa.Column("tags_progressive", sa.JSON(), nullable=True),
    )

    # Add progressive_disclosure flag to agent_apps.
    # New rows get True (default); existing rows get False via server_default.
    # We drop the server_default after setting values so future inserts use
    # the model-level default of True.
    op.add_column(
        "agent_apps",
        sa.Column(
            "progressive_disclosure",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Drop the server_default so new rows use the Python-level default (True).
    # Existing rows already have False from the migration.
    op.alter_column(
        "agent_apps",
        "progressive_disclosure",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("agent_apps", "progressive_disclosure")
    op.drop_column("tools", "tags_progressive")
    op.drop_column("tools", "summary")
