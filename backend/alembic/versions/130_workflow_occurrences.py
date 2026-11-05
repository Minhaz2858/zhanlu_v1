"""130_workflow_occurrences — workflow-schedule occurrence identity (R5/F-15).

Revision ID: 130_workflow_occurrences
Revises: 128_execution_events_leases
Create Date: 2026-11-03

WHY
---
Roadmap R5 (TS §1.3b, ADR-009): workflow schedule triggers get their OWN
occurrence table (FK to agent_apps + node_id) with identical claim/lease
mechanics through scheduler_core — never a subject_type overload of
automation occurrences. ``workflow_run_id`` provides occurrence→run
provenance. No backfill: the current workflow scheduler is stateless;
occurrences are synthesized on first adoption claim.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "130_workflow_occurrences"
down_revision: Union[str, Sequence[str], None] = "128_execution_events_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "workflow_occurrences" in inspector.get_table_names():
        return
    op.create_table(
        "workflow_occurrences",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("agent_app_id", sa.String(36),
                  sa.ForeignKey("agent_apps.id"), nullable=False),
        sa.Column("node_id", sa.String(80), nullable=False),
        sa.Column("workflow_run_id", sa.String(36), nullable=True),
        sa.Column("occurrence_key", sa.String(140), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("misfire_collapsed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("cancel_reason", sa.String(40), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_by", sa.String(80), nullable=True),
        sa.UniqueConstraint("agent_app_id", "node_id", "occurrence_key",
                            name="uq_workflow_occurrences_subject_key"),
    )
    op.create_index("ix_workflow_occurrences_agent_app_id",
                    "workflow_occurrences", ["agent_app_id"])
    op.create_index("ix_workflow_occurrences_status_sched",
                    "workflow_occurrences", ["status", "scheduled_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "workflow_occurrences" in inspector.get_table_names():
        op.drop_table("workflow_occurrences")
