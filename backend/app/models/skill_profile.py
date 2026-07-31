"""SkillProfile — extends the Tool model with governed skill metadata.

A SkillProfile represents a folder-based skill package with:
- SKILL.md (description)
- manifest.yaml (inputs, outputs, version)
- schemas (input/output validation)
- scripts (executable code)
- validators (post-execution checks)

The review pipeline (draft → in_review → approved → published → deprecated)
ensures skills are vetted before use.
"""

from typing import Optional

from sqlalchemy import String, Integer, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


# Valid skill review statuses
SKILL_REVIEW_STATUSES = [
    "draft",        # Being created
    "in_review",    # Submitted for review
    "approved",     # Reviewer approved
    "published",    # Published to marketplace
    "deprecated",   # Superseded or retired
    "rejected",     # Reviewer rejected
]

# Trust levels
TRUST_LEVELS = ["untrusted", "community", "verified", "official"]


class SkillProfile(TimestampedBase):
    """A governed skill profile — extends the Tool model with enterprise metadata.

    Every skill in the platform has a SkillProfile that records its
    manifest, review status, trust level, and package location.
    """

    __tablename__ = "skill_profiles"

    # Links
    tool_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)  # Link to Tool model

    # Identity
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[str] = mapped_column(String(50), default="1.0.0", nullable=False)

    # Package
    package_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # Folder path
    manifest: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # manifest.yaml content
    skill_md: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # SKILL.md content

    # Input/Output schemas
    input_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    output_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Review pipeline
    review_status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    trust_level: Mapped[str] = mapped_column(String(20), default="untrusted", nullable=False)
    review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    reviewed_at: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Execution
    artifact_types: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # ["pptx", "docx", ...]
    requires_sandbox: Mapped[bool] = mapped_column(default=False, nullable=False)
    sandbox_image: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Validation
    validators: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # List of validation checks
    test_cases: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Stats
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_rate: Mapped[Optional[float]] = mapped_column(nullable=True)
