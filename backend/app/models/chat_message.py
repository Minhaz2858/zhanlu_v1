"""ChatMessage model."""

from sqlalchemy import String, Text, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class ChatMessage(TimestampedBase):
    __tablename__ = "chat_messages"

    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Rich assistant content persisted so charts (report_card_payload),
    # numbered activity steps, and file artifact cards survive a page
    # refresh. The frontend streams these via the v3 SSE events; the
    # generic entity CRUD round-trips them because they are declared
    # model columns (previously ``tool_calls`` / ``activity_steps`` existed
    # in the DB but were undeclared here, so they were silently dropped on
    # write and omitted on read — the root cause of "charts gone after
    # refresh").
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    activity_steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    artifacts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Claude-style phase headline (Fathoming / Fabricating / …) so the
    # current activity-phase label survives a page refresh.  The frontend
    # streams this via the v3 ``phase`` SSE event and persists it on
    # ChatMessage.update; declaring it here makes the generic entity CRUD
    # round-trip it (otherwise _filter_data silently drops unknown keys).
    phase: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Chain-of-thought / thinking trace from reasoning-capable models.
    # Persisted so it survives page refresh and can be rendered in the
    # ReasoningPanel.  NULL for models that do not emit reasoning.
    reasoning_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Typed live activity stream events (phase_enter, tool_call_started,
    # tool_call_finished, artifact_progress, verify_passed, retry,
    # finalize_started).  Each event is a structured dict:
    #   {type, label_key, params, ts}
    # Persisted so past turns render collapsed from history and the live
    # turn resumes expanded via SSE reconnect.  Content invariant enforced
    # at emission time — never contains SQL text, raw rows, or unverified
    # totals — so this persisted log is safe by construction.
    live_events: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Data-source citations for the answer (Kimi/GPT-style source chips).
    # Each entry: {"source_id": str|None, "source_name": str, "rows": int|None}.
    # Declared so the generic entity CRUD round-trips it on write/read
    # (same pattern as tool_calls / live_events above).
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Files the USER attached to this message (Kimi/ChatGPT-style history).
    # Each entry: {"name": str, "file_url": "/api/uploads/...", "ext": str,
    # "kind": "data_file"|"html_file"|"document"|"image"}.
    # Written by the frontend on ChatMessage.create for user messages and
    # round-tripped by the generic entity CRUD so the attachment cards
    # survive a page refresh (same pattern as tool_calls / sources above).
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)
