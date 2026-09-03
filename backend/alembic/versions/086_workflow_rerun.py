"""workflow_rerun — rerun provenance on workflow_runs

Revision ID: 086_workflow_rerun
Revises: 085_a2a_tasks
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa

revision = "086_workflow_rerun"
down_revision = "085_a2a_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("rerun_of", sa.String(36), nullable=True))
    op.add_column("workflow_runs", sa.Column("rerun_from_node", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "rerun_from_node")
    op.drop_column("workflow_runs", "rerun_of")
