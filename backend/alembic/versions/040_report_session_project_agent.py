"""add session_id, project_id, project, agent_name to reports

Revision ID: 040_report_session_project_agent
Revises: 039_forecast_enhancement_phase1
Create Date: 2026-08-04
"""
from alembic import op  # type: ignore[import-untyped]
import sqlalchemy as sa  # type: ignore[import-untyped]

revision = "040_report_session_project_agent"
down_revision = "039_forecast_enhancement_phase1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reports",
                  sa.Column("session_id", sa.String(36), nullable=True))
    op.add_column("reports",
                  sa.Column("project_id", sa.String(36), nullable=True))
    op.add_column("reports",
                  sa.Column("project", sa.String(255), nullable=True))
    op.add_column("reports",
                  sa.Column("agent_name", sa.String(255), nullable=True))

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_reports_session_id ON reports (session_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_reports_project_id ON reports (project_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_reports_project_id")
    op.execute("DROP INDEX IF EXISTS ix_reports_session_id")
    op.drop_column("reports", "agent_name")
    op.drop_column("reports", "project")
    op.drop_column("reports", "project_id")
    op.drop_column("reports", "session_id")
