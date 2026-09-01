"""Add challenger_shadow_runs table for champion/challenger self-learning.

Revision ID: 074_challenger_shadow_runs
Revises: 073_merge_071_072
Create Date: 2026-08-21
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "074_challenger_shadow_runs"
down_revision = "073_merge_071_072"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "challenger_shadow_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(50), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(50), nullable=False, server_default="default-app"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        # ── Core fields ──
        sa.Column("target_id", sa.String(36), sa.ForeignKey("forecast_targets.id"), nullable=False, index=True),
        sa.Column("product_key", sa.String(100), nullable=False, index=True),
        sa.Column("challenger_type", sa.String(50), nullable=False),
        sa.Column("challenger_config", sa.JSON(), nullable=True),
        # ── Metrics ──
        sa.Column("horizon_days", sa.Integer(), nullable=False, server_default=sa.text("7")),
        sa.Column("shadow_mape", sa.Float(), nullable=True),
        sa.Column("champion_mape", sa.Float(), nullable=True),
        sa.Column("shadow_delta_mape", sa.Float(), nullable=True),
        # ── Lifecycle ──
        sa.Column("run_date", sa.DateTime(), nullable=False, index=True),
        sa.Column("promoted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade():
    op.drop_table("challenger_shadow_runs")
