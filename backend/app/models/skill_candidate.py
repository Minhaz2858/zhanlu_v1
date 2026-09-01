"""SkillCandidate — quarantine pipeline for new skill submissions.

When a skill is created via the skill factory (from description/template/trace/code),
it enters quarantine as a SkillCandidate.  It must pass review before
becoming a published SkillProfile.
"""

from typing import Optional

from sqlalchemy import String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


# Candidate statuses
CANDIDATE_STATUSES = [
    "quarantined",   # Just created, awaiting review
    "testing",       # Being tested in sandbox
    "in_review",     # Submitted for human review
    "approved",      # Approved → becomes SkillProfile
    "rejected",      # Rejected → archived
]


class SkillCandidate(TimestampedBase):
    """A skill candidate in the quarantine-review pipeline.

    Created by the skill factory when a user or agent generates a new skill.
    Must pass automated testing and human review before being published
    as a SkillProfile.
    """

    __tablename__ = "skill_candidates"

    # Source
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)  # description | template | trace | code
    source_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # Original input (description, template, etc.)

    # Generated content
    generated_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generated_manifest: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    generated_skill_md: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Review
    status: Mapped[str] = mapped_column(String(20), default="quarantined", nullable=False)
    review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Test results
    test_results: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # {passed: bool, checks: [...]}
    sandbox_job_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Link to published SkillProfile (when approved)
    published_skill_profile_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
