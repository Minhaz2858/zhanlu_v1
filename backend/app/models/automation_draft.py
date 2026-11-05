"""AutomationDraft model — server-side draft behind the chat review card.

Normative schema: docs/superpowers/specs/2026-11-03-zhanlu-automation-technical-specification.md §1.1.

The draft is the ONLY thing the LLM's ``propose_automation`` tool creates.
The canonical definition (``automation_tasks`` row) is created exclusively by
the replay-first confirm transaction (§4.2, R3) — drafts are cheap and
dedupable; only the human confirm creates a definition.

Multiple drafts per conversation are legitimate (no unique constraint beyond
the PK); the confirm arbiter lives on the definition table
(``UNIQUE (org_id, confirmation_key) WHERE is_deleted = false``).
"""

from sqlalchemy import String, Text, JSON, DateTime, Integer, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class AutomationDraft(TimestampedBase):
    __tablename__ = "automation_drafts"
    __table_args__ = (
        Index("ix_automation_drafts_org_status", "org_id", "status"),
        Index("ix_automation_drafts_expires_at", "expires_at"),
    )

    # Canonical AutomationSpec (§2) — JSONB on PostgreSQL, JSON on SQLite.
    spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Incremented on every PATCH (If-Match optimistic concurrency for confirm).
    spec_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # "draft" | "confirmed" | "discarded"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    # Client-generated UUID; copied to the definition on confirm.
    confirmation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Chat provenance.
    origin_session_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    origin_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Last server validation result ({"ok": bool, "errors": [...]}).
    validation_errors: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # TTL (default 7 days) — cleanup task reuses the scheduled-tasks pattern.
    expires_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    # Set on confirm for idempotent re-confirm (replay returns the definition).
    confirmed_definition_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Optional human-readable label the review card can show.
    draft_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def validation(self) -> dict | None:
        """Alias for ``validation_errors`` — reads as the validation result."""
        return self.validation_errors
