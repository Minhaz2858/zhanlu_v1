"""AutomationExecution model — one row per actual run of an AutomationTask.

When the dispatcher fires a task, it creates a new AutomationExecution row,
runs the agent, saves the output (and any generated files), and updates the
row with the result. The frontend reads this table to render the "Past runs"
panel in AutomationTaskDetail and to download generated files.
"""

from sqlalchemy import String, Text, JSON, ForeignKey, DateTime, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class AutomationExecution(TimestampedBase):
    __tablename__ = "automation_executions"
    __table_args__ = (
        # §1.3 identity model: one attempt row per (occurrence, attempt).
        # Partial — legacy rows (occurrence_id NULL) are exempt.
        UniqueConstraint(
            "occurrence_id", "attempt",
            name="uq_automation_executions_occurrence_attempt",
        ),
    )

    # The parent task that was fired.
    automation_task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("automation_tasks.id"), nullable=False, index=True
    )
    # Lifecycle status: "queued" / "running" / "completed" / "failed" / "skipped".
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    # When the dispatcher actually started the agent invocation.
    started_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    # When the run finished (success or failure).
    completed_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    # How long the run took in seconds (filled on completion).
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The agent's textual output (the report body, markdown, etc.). Truncated
    # for very large outputs; the full content is in the linked file rows.
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured payload (LLM JSON response, parsed tables, etc.).
    output_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Error message if status == "failed".
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which retry attempt this is (0 = first try). Incremented on each retry.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Wall-clock deadline for this run (started_at + RUN_TIMEOUT). The
    # dispatcher's janitor reaps any execution still queued/running past
    # this time, marking it failed and (if attempts remain) scheduling a
    # retry. Prevents hung LLM calls from occupying a slot forever.
    timeout_at: Mapped[object | None] = mapped_column(DateTime, nullable=True, index=True)
    # Identifier of the worker process that claimed/ran this execution
    # (diagnostics only — used to tell, from the panel, which pod ran a
    # given execution). Not a hard lease; the janitor uses timeout_at.
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Live-run observability (Manus parity): the agent streams numbered
    # activity steps ("Understanding your request", "Retrieving product prices
    # (1/3)", …) and phase headlines ("Fathoming", "Fabricating", …) as it
    # works. The executor mirrors these here so the Scheduled panel can
    # render a live plan checklist by polling /executions/{id}/details
    # instead of showing a bare "running" spinner until completion.
    activity_steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    current_phase: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # The chat session the result was sent to (if any).
    notified_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("chat_sessions.id"), nullable=True
    )
    # Recursion chain: when an automation run is spawned from inside another
    # run (via the ``execute_automation`` tool), this points at the spawning
    # execution. NULL for top-level scheduled/manual runs. Used to cap nesting
    # depth (see ``automation_dispatcher.compute_execution_depth``) so a
    # self-triggering task can't fan out infinitely. Plain VARCHAR at the
    # storage layer (no DB-level FK) — the depth walk is enforced in app code.
    parent_execution_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True,
        doc="Parent execution when spawned via execute_automation; "
            "NULL for top-level runs. Caps recursion depth.",
    )
    # When the email notification gateway sent the run's result email
    # (success or failure). NULL when no email was sent (disabled, no
    # recipients, or notify_on mismatch). Used for dedup + observability.
    email_notified_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    # ── R5 occurrence identity + cancellation metadata (TS §1.3) ────────
    # Logical occurrence identity: executions are ATTEMPTS against an
    # occurrence; UNIQUE(occurrence_id, attempt) enforces one attempt row
    # per attempt number. NULL for legacy rows (backfilled idempotently in
    # migration 127).
    occurrence_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Chains attempts WITHIN one occurrence (retry #1 points at attempt 0's
    # execution). parent_execution_id (above) chains recursion across
    # occurrences — the two are distinct by design (audit fix F-13-review).
    retry_of_execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Typed cancellation metadata (§3.2): user | admin | definition_deleted |
    # org_deactivated | parent_cancelled | superseded.
    cancel_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cancelled_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # R5 persisted-retry scheduling: run-when for queued retry rows (the
    # in-memory backoff timer is retired). NULL for fresh fires.
    next_attempt_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
