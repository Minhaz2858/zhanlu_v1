"""R5 durable scheduling plane — occurrences, leases, events, delivery, ledger.

Normative schemas (docs/superpowers/specs/2026-11-03-zhanlu-automation-technical-specification.md):
§1.3a ``automation_occurrences`` (migration 127), §1.3b ``workflow_occurrences``
(migration 130), §1.4 ``automation_execution_events`` (migration 128, F-26
attempt-scoped identity), §1.5 ``execution_leases`` (128),
§1.5b ``automation_delivery_events`` (128, F-13), §1.5c
``automation_side_effect_ops`` (128, F-27).

All tables are additive; legacy rows backfill inside the migrations
(idempotent). Claim/lease/fencing/recovery mechanics live in
``app/services/automation/scheduler_core.py``.
"""

from sqlalchemy import (
    String, Text, JSON, ForeignKey, DateTime, Integer, Boolean, Index, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampedBase


class AutomationOccurrence(TimestampedBase):
    """§1.3a — the logical identity of one "time the automation should run".

    Executions are attempts against an occurrence; the occurrence survives
    retries and is the duplicate-claim arbiter
    (UNIQUE (automation_task_id, occurrence_key)).
    """
    __tablename__ = "automation_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "automation_task_id", "occurrence_key",
            name="uq_automation_occurrences_task_key",
        ),
        Index("ix_automation_occurrences_status_sched", "status", "scheduled_at"),
    )

    automation_task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("automation_tasks.id"), nullable=False, index=True
    )
    # "scheduled:{scheduled_time_utc}" | "manual:{ulid}" — dedup identity.
    occurrence_key: Mapped[str] = mapped_column(String(140), nullable=False)
    # Intended fire time (UTC).
    scheduled_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    # pending → claimed → completed | failed_terminal | cancelled | skipped
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # Denormalized from executions.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Missed slots collapsed into this row (§6.1a).
    misfire_collapsed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    # Typed reason when status=cancelled (§3.2) — a PENDING occurrence can be
    # cancelled without any execution row existing.
    cancel_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cancelled_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class WorkflowOccurrence(TimestampedBase):
    """§1.3b — workflow-schedule occurrences (migration 130).

    Separate resource from automation occurrences (FK to agent_apps, not
    automation_tasks) but the SAME claim/lease/fencing mechanics through
    ``scheduler_core`` — never a subject_type overload.
    """
    __tablename__ = "workflow_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "agent_app_id", "node_id", "occurrence_key",
            name="uq_workflow_occurrences_subject_key",
        ),
        Index("ix_workflow_occurrences_status_sched", "status", "scheduled_at"),
    )

    agent_app_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_apps.id"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(80), nullable=False)
    # Set by the workflow executor when a run starts — occurrence→run
    # provenance. No backfill: the current scheduler is stateless.
    workflow_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    occurrence_key: Mapped[str] = mapped_column(String(140), nullable=False)
    scheduled_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    misfire_collapsed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cancelled_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ExecutionLease(Base):
    """§1.5 — fencing leases for RUNNING executions (migration 128).

    One row per execution (PK = execution_id — deliberately NOT the shared
    TimestampedBase, whose uuid PK would break the one-lease-per-execution
    invariant). The fencing_token is compared on every state write;
    heartbeats renew lease_expires_at.
    """
    __tablename__ = "execution_leases"

    execution_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("automation_executions.id"),
        primary_key=True,
    )
    owner: Mapped[str] = mapped_column(String(120), nullable=False)
    lease_expires_at: Mapped[object] = mapped_column(DateTime, nullable=False, index=True)
    # Monotonic per execution (ULID/timestamp-based).
    fencing_token: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    created_date: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    updated_date: Mapped[object | None] = mapped_column(DateTime, nullable=True)


class AutomationExecutionEvent(TimestampedBase):
    """§1.4 — append-only observability ledger (migration 128).

    F-26: identity is attempt-scoped — UNIQUE (execution_id, attempt, seq).
    Different attempts restart seq at 0; cross-attempt reuse is legal and
    cannot collide. Replaces in-place ``activity_steps`` mutation (column
    retained read-only for one release).
    """
    __tablename__ = "automation_execution_events"
    __table_args__ = (
        UniqueConstraint(
            "execution_id", "attempt", "seq",
            name="uq_automation_execution_events_attempt_seq",
        ),
        Index("ix_automation_execution_events_exec", "execution_id", "attempt", "seq"),
    )

    execution_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("automation_executions.id"), nullable=False
    )
    # The attempt this event belongs to (F-26: sequences are per-attempt).
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Monotonic per (execution_id, attempt).
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    occurred_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)


class AutomationDeliveryEvent(TimestampedBase):
    """§1.5b — durable per-channel delivery claims (migration 128, F-13).

    Execution terminality and delivery success are independent lifecycles.
    """
    __tablename__ = "automation_delivery_events"
    __table_args__ = (
        UniqueConstraint(
            "execution_id", "channel", name="uq_automation_delivery_events_exec_channel"
        ),
        Index("ix_automation_delivery_events_status", "status", "claim_expires_at"),
    )

    execution_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("automation_executions.id"), nullable=False
    )
    # "chat" | "email" | "panel"
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    # "{execution_id}:{channel}" — provider-facing where supported.
    idempotency_key: Mapped[str] = mapped_column(String(140), nullable=False)
    # pending → claimed → delivered | failed_permanent | unknown
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # ULID minted at claim; compared on state writes (delivery-side fencing).
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Claim lease (default 5 min); expired claims are re-claimable.
    claim_expires_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)


class AutomationSideEffectOp(TimestampedBase):
    """§1.5c — intent-and-outcome ledger for external side effects (128, F-27).

    Sole source of truth for side-effect state: missing ``output_text``
    says nothing about side effects — only this ledger can prove (or
    disprove) that an external operation occurred.
    """
    __tablename__ = "automation_side_effect_ops"
    __table_args__ = (
        UniqueConstraint(
            "execution_id", "attempt", "op_key",
            name="uq_automation_side_effect_ops_attempt_op",
        ),
        Index("ix_automation_side_effect_ops_status", "status", "updated_date"),
    )

    execution_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("automation_executions.id"), nullable=False
    )
    # Attempt that (was to) perform the op.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # "{attempt}:{tool_call_seq}:{tool_name}" — operation identity.
    op_key: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Gate B key "{execution_id}:{attempt}:{tool_call_seq}" — supplied to
    # the provider where supported.
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # dispatched → started → confirmed_complete | failed
    # (reconciliation may set confirmed_not_started | unknown)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="dispatched")
    reconciled_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    # provider_check | ledger_replay | operator | timeout_ageout
    reconciled_via: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Idempotent (Gate B) — unknown outcome + idempotent ⇒ retry allowed.
    idempotent: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
