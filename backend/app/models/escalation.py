"""Escalations (P3-6) — structured human handoff with full context.

When an agent can't resolve an issue, senses frustration, or the user
asks for a human, it creates an escalation: a packaged handoff (summary,
what was tried, collected data, sentiment) instead of a dead end. IBM's
#1 chatbot best practice: easy escalation WITH full conversational
context transfer.
"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampedBase


class Escalation(TimestampedBase, Base):
    __tablename__ = "escalations"

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # what the agent already tried / collected — the "context transfer"
    context_json: Mapped[dict | None] = mapped_column(Text, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # optional ontology object whose linked domains get notified
    object_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # self-reported by the agent: frustrated | asks_human | stuck | negative
    sentiment: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
