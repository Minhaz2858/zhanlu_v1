"""HarnessRuleEvent — one row per firing of a deterministic (hardcoded) rule.

Phase 0 of the Agent Harness migration: the ~12 functions that read the user's
text and decide agent behaviour cannot be deleted safely until we can show, per
rule, how often it fires and what it forced. This table is that evidence.

Written best-effort by ``app/services/harness/rule_events.py``; a write failure
must never affect a turn (see that module's fail-open contract).
"""

from sqlalchemy import Boolean, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class HarnessRuleEvent(TimestampedBase):
    __tablename__ = "harness_rule_events"

    # Dotted rule identity, e.g. "file_turn_guard.should_force_create_file".
    rule_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    # True when the rule changed the outcome (forced/classified as yes).
    fired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The raw decision, stringified and clipped — tuples/lists are recorded whole.
    decision: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # What the rule forced, when the rule is a forcing rule (artifact/dashboard/…).
    forced_target: Mapped[str | None] = mapped_column(String(80), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    turn_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    surface: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Free-form context: iteration, cap, matched fragment, divergence info (Phase 2).
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> dict:
        return super().to_dict()
