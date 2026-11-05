"""132_automation_retry_scheduling — persisted retry run-when (R5).

Revision ID: 132_automation_retry_scheduling
Revises: 130_workflow_occurrences
Create Date: 2026-11-03

WHY
---
Roadmap R5: retire the in-memory ``_delayed_run`` retry timer (process
death loses the retry). Retries are now persisted rows
(``status='queued'``, ``retry_of_execution_id`` set) whose run-when lives
in ``next_attempt_at``; the tick promotes them when due. Per TS §1.7 the
number ≥132 is taken at implementation time (129/131 are committed to R8/R9
in the approved plan and are never pre-assigned).

NOTE on chain topology: this revision chains 130 → 132. Migrations 129 (R8)
and 131 (R9) must be spliced into the chain at their implementation time —
the final linear chain is 128→129→130→131→132; each splice rewrites the
downstream file's ``down_revision`` pointer (both 130 and 132 carry this
note; R8/R9 implementers MUST rewire, not fork).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "132_automation_retry_scheduling"
down_revision: Union[str, Sequence[str], None] = "130_workflow_occurrences"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_column(inspector, "automation_executions", "next_attempt_at"):
        op.add_column(
            "automation_executions",
            sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        )
    inspector = sa.inspect(bind)
    existing = {i["name"] for i in inspector.get_indexes("automation_executions")}
    if "ix_automation_executions_next_attempt_at" not in existing:
        op.create_index(
            "ix_automation_executions_next_attempt_at",
            "automation_executions", ["next_attempt_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {i["name"] for i in inspector.get_indexes("automation_executions")}
    if "ix_automation_executions_next_attempt_at" in existing:
        op.drop_index("ix_automation_executions_next_attempt_at",
                      table_name="automation_executions")
    inspector = sa.inspect(bind)
    if _has_column(inspector, "automation_executions", "next_attempt_at"):
        op.drop_column("automation_executions", "next_attempt_at")
