"""127_automation_occurrences — R5 occurrence identity + execution changes.

Revision ID: 127_automation_occurrences
Revises: 126_automation_spec_drafts
Create Date: 2026-11-03

WHY
---
Roadmap R5 (TS §1.3a, §1.3): durable occurrence identity — executions
become attempts against an occurrence; UNIQUE(automation_task_id,
occurrence_key) is the duplicate-claim arbiter; UNIQUE(occurrence_id,
attempt) enforces one attempt row per attempt number (partial — legacy
rows with NULL occurrence_id are exempt, since NULLs never collide in a
unique index). Also carries the R4 trigger-fidelity columns
(``trigger_type``, ``run_at``, ``schedule_degraded``, ``schedule_error``)
per the TS §1.7 single-chain ownership (R4 has no migration of its own;
127 is the next free number).

Backfill (idempotent): legacy IN-FLIGHT executions (queued/running with
NULL occurrence_id) get a synthesized claimed occurrence — a restart after
this migration must not orphan a live run. Historic terminal rows stay
NULL (occurrence identity begins at adoption; the dispatcher only ever
synthesizes on in-flight rows, so duplicates cannot arise).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = "127_automation_occurrences"
down_revision: Union[str, Sequence[str], None] = "126_automation_spec_drafts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_table(inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ── 1. automation_occurrences (§1.3a) ───────────────────────────────
    if not _has_table(inspector, "automation_occurrences"):
        op.create_table(
            "automation_occurrences",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
            sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
            sa.Column("created_by_id", sa.String(36), nullable=True),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_date", sa.DateTime(), nullable=True),
            sa.Column("updated_date", sa.DateTime(), nullable=True),
            sa.Column("automation_task_id", sa.String(36),
                      sa.ForeignKey("automation_tasks.id"), nullable=False),
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
            sa.UniqueConstraint(
                "automation_task_id", "occurrence_key",
                name="uq_automation_occurrences_task_key",
            ),
        )
        op.create_index(
            "ix_automation_occurrences_automation_task_id",
            "automation_occurrences", ["automation_task_id"],
        )
        op.create_index(
            "ix_automation_occurrences_status_sched",
            "automation_occurrences", ["status", "scheduled_at"],
        )

    # ── 2. automation_executions: occurrence identity + cancellation ────
    inspector = sa.inspect(bind)
    for col, ddl in (
        ("occurrence_id", sa.Column("occurrence_id", sa.String(36), nullable=True)),
        ("retry_of_execution_id", sa.Column("retry_of_execution_id", sa.String(36), nullable=True)),
        ("cancel_reason", sa.Column("cancel_reason", sa.String(40), nullable=True)),
        ("cancelled_at", sa.Column("cancelled_at", sa.DateTime(), nullable=True)),
        ("cancelled_by", sa.Column("cancelled_by", sa.String(80), nullable=True)),
    ):
        if not _has_column(inspector, "automation_executions", col):
            op.add_column("automation_executions", ddl)
    inspector = sa.inspect(bind)
    existing_idx = {i["name"] for i in inspector.get_indexes("automation_executions")}
    if "uq_automation_executions_occurrence_attempt" not in existing_idx:
        if bind.dialect.name == "postgresql":
            op.create_index(
                "uq_automation_executions_occurrence_attempt",
                "automation_executions", ["occurrence_id", "attempt"],
                unique=True,
                postgresql_where=sa.text("occurrence_id IS NOT NULL"),
            )
        else:
            op.create_index(
                "uq_automation_executions_occurrence_attempt",
                "automation_executions", ["occurrence_id", "attempt"],
                unique=True,
                sqlite_where=sa.text("occurrence_id IS NOT NULL"),
            )
    op.create_index(
        "ix_automation_executions_occurrence_id",
        "automation_executions", ["occurrence_id"],
    )

    # ── 3. R4 trigger-fidelity columns (single-chain ownership, see docstring)
    inspector = sa.inspect(bind)
    if not _has_column(inspector, "automation_tasks", "trigger_type"):
        op.add_column(
            "automation_tasks",
            sa.Column("trigger_type", sa.String(20), nullable=True),
        )
    if not _has_column(inspector, "automation_tasks", "run_at"):
        op.add_column("automation_tasks", sa.Column("run_at", sa.DateTime(), nullable=True))
    if not _has_column(inspector, "automation_tasks", "schedule_degraded"):
        op.add_column(
            "automation_tasks",
            sa.Column("schedule_degraded", sa.Boolean(), nullable=True, server_default=sa.text("false")),
        )
    if not _has_column(inspector, "automation_tasks", "schedule_error"):
        op.add_column("automation_tasks", sa.Column("schedule_error", sa.Text(), nullable=True))

    # ── 4. Backfill: legacy in-flight executions → synthesized occurrence
    # (idempotent: only rows with occurrence_id IS NULL and non-terminal).
    backfill = text("""
        INSERT INTO automation_occurrences
            (id, org_id, app_id, is_deleted, created_date, updated_date,
             automation_task_id, occurrence_key, scheduled_at, status,
             attempt_count, misfire_collapsed_count, claimed_at)
        SELECT e.id, e.org_id, e.app_id, false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
               e.automation_task_id,
               'legacy:' || e.id, e.created_date, 'claimed',
               1, 0, CURRENT_TIMESTAMP
        FROM automation_executions e
        WHERE e.occurrence_id IS NULL
          AND e.status IN ('queued', 'running')
          AND NOT EXISTS (
              SELECT 1 FROM automation_occurrences o WHERE o.id = e.id
          )
    """)
    bind.execute(backfill)
    bind.execute(text("""
        UPDATE automation_executions
        SET occurrence_id = id
        WHERE occurrence_id IS NULL AND status IN ('queued', 'running')
    """))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_idx = {i["name"] for i in inspector.get_indexes("automation_executions")}
    if "ix_automation_executions_occurrence_id" in existing_idx:
        op.drop_index("ix_automation_executions_occurrence_id", table_name="automation_executions")
    if "uq_automation_executions_occurrence_attempt" in existing_idx:
        op.drop_index("uq_automation_executions_occurrence_attempt", table_name="automation_executions")
    inspector = sa.inspect(bind)
    for col in ("cancelled_by", "cancelled_at", "cancel_reason",
                "retry_of_execution_id", "occurrence_id"):
        if _has_column(inspector, "automation_executions", col):
            op.drop_column("automation_executions", col)
    inspector = sa.inspect(bind)
    for col in ("schedule_error", "schedule_degraded", "run_at", "trigger_type"):
        if _has_column(inspector, "automation_tasks", col):
            op.drop_column("automation_tasks", col)
    if _has_table(inspector, "automation_occurrences"):
        op.drop_table("automation_occurrences")
