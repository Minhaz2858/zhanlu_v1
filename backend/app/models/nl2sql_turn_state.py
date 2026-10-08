"""Nl2sqlTurnState — structured multi-turn planning state (OCI parity, P1-6).

The OCI reference design treats multi-turn interaction as structured state,
not appended chat text: each turn carries forward resolved entities, accepted
defaults and rejected interpretations so the planner narrows retrieval instead
of restarting from zero.

One row per (conversation, knowledge base). Written by the NL2SQL planner
(``services/db/nl2sql_planner.py``) after a clarification is resolved or a
successful answer finalizes its assumptions; read at the start of the next
planning pass in the same conversation.

Follows the metadata / create_all convention of ``query_plan_memory.py`` —
the table is created on backend startup, no alembic revision required.
"""

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class Nl2sqlTurnState(TimestampedBase):
    """Resolved planning context for one (conversation, KB) pair."""

    __tablename__ = "nl2sql_turn_state"

    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kb_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # Entities the user confirmed this conversation is about, e.g.
    # {"entity": "OCI", "metric": "revenue", "scope": "North America"}.
    resolved: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Defaults the planner applied WITHOUT asking (surfaced as assumptions);
    # the next turn may reuse them silently instead of re-deriving.
    defaults: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Interpretations offered in a clarification that the user rejected, so
    # the planner does not offer them twice.
    rejected: Mapped[list | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("conversation_id", "kb_id", name="uq_nl2sql_turn_state"),
    )
