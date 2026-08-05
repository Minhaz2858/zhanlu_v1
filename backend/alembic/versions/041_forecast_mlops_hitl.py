"""forecast mlops + hitl: forecast_feedback, forecast_weight_adjustments, run_id

Revision ID: 041_forecast_mlops_hitl
Revises: 040_report_session_project_agent
Create Date: 2026-08-05
"""
from alembic import op  # type: ignore[import-untyped]
import sqlalchemy as sa  # type: ignore[import-untyped]

revision = "041_forecast_mlops_hitl"
down_revision = "040_report_session_project_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Link realized-eval rows back to their ForecastRun.
    op.add_column("forecast_accuracy_log",
                  sa.Column("run_id", sa.String(36), nullable=True))
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_forecast_accuracy_log_run_id "
        "ON forecast_accuracy_log (run_id)"
    )

    # 2. HITL feedback table.
    op.create_table(
        "forecast_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_id", sa.String(36),
                  sa.ForeignKey("forecast_targets.id"), nullable=False),
        sa.Column("product_id", sa.String(100), nullable=False),
        sa.Column("ai_price", sa.Float, nullable=False),
        sa.Column("user_price", sa.Float, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("author_id", sa.String(36), nullable=False),
        sa.Column("author_name", sa.String(255), nullable=True),
        sa.Column("target_date", sa.DateTime, nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("ai_error", sa.Float, nullable=True),
        sa.Column("user_error", sa.Float, nullable=True),
        sa.Column("beat", sa.Boolean, nullable=True),
        sa.Column("scored_at", sa.DateTime, nullable=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("app_id", sa.String(36), nullable=False),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_forecast_feedback_target_id ON forecast_feedback (target_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_forecast_feedback_product_id ON forecast_feedback (product_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_forecast_feedback_author_id ON forecast_feedback (author_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_forecast_feedback_status ON forecast_feedback (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_forecast_feedback_target_date ON forecast_feedback (target_date)")

    # 3. Weight-adjustment audit table.
    op.create_table(
        "forecast_weight_adjustments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_id", sa.String(36),
                  sa.ForeignKey("forecast_targets.id"), nullable=False),
        sa.Column("triggered_by", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("old_weights", sa.JSON, nullable=True),
        sa.Column("new_weights", sa.JSON, nullable=True),
        sa.Column("delta_ratio", sa.Float, nullable=True),
        sa.Column("applied", sa.Boolean, nullable=False),
        sa.Column("applied_at", sa.DateTime, nullable=True),
        sa.Column("created_date", sa.DateTime, nullable=True),
        sa.Column("updated_date", sa.DateTime, nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("app_id", sa.String(36), nullable=False),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_fwa_target_id ON forecast_weight_adjustments (target_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_fwa_triggered_by ON forecast_weight_adjustments (triggered_by)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_fwa_triggered_by")
    op.execute("DROP INDEX IF EXISTS ix_fwa_target_id")
    op.drop_table("forecast_weight_adjustments")
    op.execute("DROP INDEX IF EXISTS ix_forecast_feedback_target_date")
    op.execute("DROP INDEX IF EXISTS ix_forecast_feedback_status")
    op.execute("DROP INDEX IF EXISTS ix_forecast_feedback_author_id")
    op.execute("DROP INDEX IF EXISTS ix_forecast_feedback_product_id")
    op.execute("DROP INDEX IF EXISTS ix_forecast_feedback_target_id")
    op.drop_table("forecast_feedback")
    op.execute("DROP INDEX IF EXISTS ix_forecast_accuracy_log_run_id")
    op.drop_column("forecast_accuracy_log", "run_id")
