"""LearningProposal — proposed improvement backed by experience entries.

When the learning pipeline identifies a pattern from experience entries,
it creates a LearningProposal.  Proposals go through review (proposed →
approved → applied or rejected) and track the resulting impact.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Float, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

PROPOSAL_TYPES = [
    "prompt_optimization", "tool_refinement", "skill_update",
    "policy_update", "context_update", "agent_config",
]
PROPOSAL_STATUSES = ["proposed", "reviewing", "approved", "applied", "rejected", "rollback"]


class LearningProposal(TimestampedBase):
    """A proposed improvement from experience-based learning."""

    __tablename__ = "learning_proposals"

    # References
    experience_entry_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    agent_app_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    skill_profile_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Proposal
    proposal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    before_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    after_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expected_impact: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Review
    status: Mapped[str] = mapped_column(String(20), default="proposed", nullable=False)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Application
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    applied_result: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
