"""AutomationTask model — scheduled task with cron + natural language support.

A scheduled task that, when fired, runs the agent to produce a report/file
output. Inspired by Manus AI's "Scheduled" feature: a task carries a cron
expression (or human-readable schedule) and the dispatcher in
``app/services/automation_dispatcher.py`` picks it up at the next run time.
"""

from sqlalchemy import String, Text, JSON, ForeignKey, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class AutomationTask(TimestampedBase):
    __tablename__ = "automation_tasks"

    # Canonical status vocabulary — the single source of truth for the LLM
    # create tool, the dispatcher self-heal sweep, the optional DB CHECK
    # constraint, and tests. Never persist anything outside this set.
    VALID_STATUSES = ("active", "paused", "failed", "completed")

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    project: Mapped[str | None] = mapped_column(String(255), nullable=True, default="global")
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Human-readable description of what the user wants (e.g. "i want to see c5
    # product price weekly"). Used as the agent prompt at execution time.
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form schedule string as supplied by the user/agent — could be cron
    # ("0 9 * * 1") or natural language ("every Monday at 9 AM").
    schedule: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Normalized cron expression (5-field standard). Set by the dispatcher
    # after parse_schedule() runs. If null, the task is manual-only.
    cron_expression: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # IANA timezone name (e.g. "Asia/Shanghai", "America/New_York") in which
    # the cron schedule is interpreted, so "daily 08:00" fires at 08:00
    # user-local — not 08:00 UTC (P0-6). Defaults to UTC. The dispatcher
    # converts the tz-local next occurrence to UTC-naive for storage, so
    # next_run_at is always UTC and the existing UTC comparison is unchanged.
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True, default="UTC")
    # Next wall-clock UTC time the task should fire.
    next_run_at: Mapped[object | None] = mapped_column(DateTime, nullable=True, index=True)
    # Last time the dispatcher actually started an execution.
    last_run_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    # "active" / "paused" / "failed" / "completed" (one-shot tasks).
    status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="paused")
    last_run: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    # List of past runs as lightweight dicts (legacy field; new
    # AutomationExecution rows are the source of truth).
    execution_history: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Output format hint — "html" (default), "pptx", "docx", "pdf", "json".
    output_format: Mapped[str | None] = mapped_column(String(30), nullable=True, default="html")
    # Whether to auto-send the result to the user's chat when the task runs.
    notify_chat: Mapped[bool | None] = mapped_column(
        String(10), nullable=True, default="true"
    )
    # Max retry attempts on transient failures (per execution).
    max_retries: Mapped[int | None] = mapped_column(
        String(10), nullable=True, default="2"
    )
    # Whether to skip the user confirmation step (Manus "Always skip").
    skip_confirmation: Mapped[bool | None] = mapped_column(
        String(10), nullable=True, default="false"
    )
    # Agent to use when running this task. If null, the system picks the
    # first available agent in the workspace.
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Origin chat session — set by Chat.jsx when the agent creates the
    # task via the structured ``create_resource`` field. Used by the
    # sidebar to surface a small clock icon on the originating session
    # so users can easily find the "control room" for an automation.
    # Nullable because (1) automations created outside the chat flow
    # (e.g. from the My Space automation page) have no origin session,
    # and (2) backfilling existing rows is not necessary for the icon
    # to start showing on new automations.
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("chat_sessions.id"), nullable=True, index=True
    )
    # Bound knowledge base / data source. Optional — agents that need to
    # read from a specific database use this to scope their `ask_data_agent`
    # queries without the LLM having to pick from a list at create time.
    # Set automatically when the user creates the automation from inside a
    # project that has exactly one bound data source, or via the explicit
    # ``data_source_id`` arg in the create_automation tool call.
    data_source_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id"), nullable=True, index=True
    )
    # Skills enabled for this automation (ordered JSON array of skill names).
    # The executor injects a compact metadata index (progressive disclosure)
    # and the agent loads full SKILL.md bodies on demand via the `skills` /
    # `load_skill_body` tool. Nullable to match agent_apps.skills; read paths
    # normalize None -> [] so legacy rows behave like "no skills enabled".
    skills: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # ── Email notification gateway (per-task settings) ───────────────────
    # notify_enabled: per-task master switch. When False (default) no email is
    #   sent regardless of the other fields; the user must explicitly opt in.
    # notify_emails: JSON array of recipient addresses. Empty/None disables
    #   email for this task (the gateway itself is gated by
    #   NOTIFICATION_GATEWAY_ENABLED at the settings level).
    # notify_on: "always" (default) | "on_success" | "on_failure".
    # attach_file: when True (default), attach the run's output file if it is
    #   under EMAIL_ATTACH_MAX_BYTES; otherwise (or when the file is too big)
    #   a time-limited HMAC-signed download link is included instead.
    notify_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    notify_emails: Mapped[list | None] = mapped_column(JSON, nullable=True)
    notify_on: Mapped[str | None] = mapped_column(String(20), nullable=True, default="always")
    attach_file: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    # ── LLM-informed tick (2026-08-27) ──────────────────────────────────
    # When True, the dispatcher enriches each fired execution with a
    # lightweight LLM-generated "context preamble" (last run summary +
    # fresh prompt interpretation) before the deterministic executor runs.
    # Default False — keeps scheduled runs cheap and reliable (the
    # dispatcher's "No LLM in the tick" guarantee is preserved unless the
    # user explicitly opts in for Kimi-style smart scheduled research).
    llm_informed_tick: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=False
    )
