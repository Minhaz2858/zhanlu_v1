"""128_execution_events_leases_delivery_ledger — R5 execution-plane tables.

Revision ID: 128_execution_events_leases
Revises: 127_automation_occurrences
Create Date: 2026-11-03

WHY
---
Roadmap R5 (TS §1.4, §1.5, §1.5b, §1.5c):
- ``automation_execution_events`` — append-only event ledger with the F-26
  attempt-scoped identity UNIQUE(execution_id, attempt, seq);
- ``execution_leases`` — fencing leases for running executions;
- ``automation_delivery_events`` — F-13 durable delivery claims;
- ``automation_side_effect_ops`` — F-27 side-effect ledger (sole source of
  truth for side-effect state; powers the pre-retry reconciliation barrier).

All additive; no backfill (event/delivery/ledger rows begin at adoption).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "128_execution_events_leases"
down_revision: Union[str, Sequence[str], None] = "127_automation_occurrences"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table: str) -> bool:
    return table in inspector.get_table_names()


_BASE_COLS = [
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
    sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
    sa.Column("created_by_id", sa.String(36), nullable=True),
    sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    sa.Column("created_date", sa.DateTime(), nullable=True),
    sa.Column("updated_date", sa.DateTime(), nullable=True),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "automation_execution_events"):
        op.create_table(
            "automation_execution_events",
            *_BASE_COLS,
            sa.Column("execution_id", sa.String(36),
                      sa.ForeignKey("automation_executions.id"), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(40), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("agent_run_id", sa.String(36), nullable=True),
            sa.Column("occurred_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("execution_id", "attempt", "seq",
                                name="uq_automation_execution_events_attempt_seq"),
        )
        op.create_index("ix_automation_execution_events_exec",
                        "automation_execution_events",
                        ["execution_id", "attempt", "seq"])

    if not _has_table(inspector, "execution_leases"):
        op.create_table(
            "execution_leases",
            sa.Column("execution_id", sa.String(36),
                      sa.ForeignKey("automation_executions.id"), primary_key=True),
            sa.Column("owner", sa.String(120), nullable=False),
            sa.Column("lease_expires_at", sa.DateTime(), nullable=False),
            sa.Column("fencing_token", sa.String(64), nullable=False),
            sa.Column("acquired_at", sa.DateTime(), nullable=True),
            sa.Column("created_date", sa.DateTime(), nullable=True),
            sa.Column("updated_date", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_execution_leases_lease_expires_at",
                        "execution_leases", ["lease_expires_at"])

    if not _has_table(inspector, "automation_delivery_events"):
        op.create_table(
            "automation_delivery_events",
            *_BASE_COLS,
            sa.Column("execution_id", sa.String(36),
                      sa.ForeignKey("automation_executions.id"), nullable=False),
            sa.Column("channel", sa.String(20), nullable=False),
            sa.Column("idempotency_key", sa.String(140), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("claim_token", sa.String(64), nullable=True),
            sa.Column("claim_owner", sa.String(120), nullable=True),
            sa.Column("claim_expires_at", sa.DateTime(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("execution_id", "channel",
                                name="uq_automation_delivery_events_exec_channel"),
        )
        op.create_index("ix_automation_delivery_events_status",
                        "automation_delivery_events", ["status", "claim_expires_at"])

    if not _has_table(inspector, "automation_side_effect_ops"):
        op.create_table(
            "automation_side_effect_ops",
            *_BASE_COLS,
            sa.Column("execution_id", sa.String(36),
                      sa.ForeignKey("automation_executions.id"), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("op_key", sa.String(160), nullable=False),
            sa.Column("tool_name", sa.String(120), nullable=True),
            sa.Column("idempotency_key", sa.String(160), nullable=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="dispatched"),
            sa.Column("reconciled_at", sa.DateTime(), nullable=True),
            sa.Column("reconciled_via", sa.String(40), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("idempotent", sa.Boolean(), nullable=True, server_default=sa.text("false")),
            sa.UniqueConstraint("execution_id", "attempt", "op_key",
                                name="uq_automation_side_effect_ops_attempt_op"),
        )
        op.create_index("ix_automation_side_effect_ops_status",
                        "automation_side_effect_ops", ["status", "updated_date"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in (
        "automation_side_effect_ops",
        "automation_delivery_events",
        "execution_leases",
        "automation_execution_events",
    ):
        if _has_table(inspector, table):
            op.drop_table(table)
