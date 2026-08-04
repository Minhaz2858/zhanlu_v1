"""forecast enhancement phase 1 columns

Revision ID: 039_forecast_enhancement_phase1
Revises: 038_resource_shares_and_type
Create Date: 2026-08-03
"""
from alembic import op  # type: ignore[import-untyped]
import sqlalchemy as sa  # type: ignore[import-untyped]

revision = "039_forecast_enhancement_phase1"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── ForecastRun extensions ──────────────────────────────
    op.add_column("forecast_runs", sa.Column("exog_features_used", sa.JSON(), nullable=True))
    op.add_column("forecast_runs", sa.Column("cleaning_notes", sa.Text(), nullable=True))
    op.add_column("forecast_runs", sa.Column("explanation", sa.JSON(), nullable=True))
    op.add_column("forecast_runs", sa.Column("cleaning_report", sa.JSON(), nullable=True))
    op.add_column("forecast_runs", sa.Column("coherence_report", sa.JSON(), nullable=True))
    op.add_column("forecast_runs", sa.Column(
        "exog_degraded", sa.Boolean(), nullable=False, server_default=sa.text("false")
    ))

    # ── ForecastAccuracyLog extensions ──────────────────────
    op.add_column("forecast_accuracy_log", sa.Column("realized_error", sa.Float(), nullable=True))
    op.add_column("forecast_accuracy_log", sa.Column("realized_mape", sa.Float(), nullable=True))
    op.add_column("forecast_accuracy_log", sa.Column("evaluated_at", sa.DateTime(), nullable=True))
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_forecast_accuracy_log_evaluated_at
        ON forecast_accuracy_log (evaluated_at)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_forecast_accuracy_log_evaluated_at")
    op.drop_column("forecast_accuracy_log", "evaluated_at")
    op.drop_column("forecast_accuracy_log", "realized_mape")
    op.drop_column("forecast_accuracy_log", "realized_error")
    op.drop_column("forecast_runs", "exog_degraded")
    op.drop_column("forecast_runs", "coherence_report")
    op.drop_column("forecast_runs", "cleaning_report")
    op.drop_column("forecast_runs", "explanation")
    op.drop_column("forecast_runs", "cleaning_notes")
    op.drop_column("forecast_runs", "exog_features_used")
