"""ExperienceEntry — feedback-driven learning record from executions.

Captures the outcome of agent/skill executions along with user ratings,
feedback text, and structured detail.  These entries feed the learning
pipeline that surfaces patterns and proposes improvements.
"""

from typing import Optional

from sqlalchemy import String, Integer, Float, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

EXPERIENCE_OUTCOMES = ["success", "partial", "failure", "timeout", "cancelled"]
EXPERIENCE_ENTRY_TYPES = ["execution", "skill_run", "tool_call", "user_feedback", "review", "test_result", "role_relevance_feedback"]


class ExperienceEntry(TimestampedBase):
    """A learning feedback entry — outcome, rating, feedback, metadata."""

    __tablename__ = "experience_entries"

    # References
    execution_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    skill_run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    agent_app_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Entry type
    entry_type: Mapped[str] = mapped_column(String(30), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    detail_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Feedback signal
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    user_rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    user_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Metadata
    tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    embedding_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
