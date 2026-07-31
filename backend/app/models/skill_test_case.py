"""SkillTestCase — test cases for skill verification.

Tests a skill with known inputs and expected outputs.  Supports
schema validation checks and can be executed in sandbox environments.
Tracks pass/fail history and run counts.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Boolean, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

SKILL_TEST_TYPES = ["unit", "integration", "schema", "performance", "regression"]
SKILL_TEST_RESULTS = ["pending", "pass", "fail", "error", "timeout"]


class SkillTestCase(TimestampedBase):
    """A test case that verifies skill behavior with known inputs/outputs."""

    __tablename__ = "skill_test_cases"

    # References
    skill_profile_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    skill_candidate_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Test spec
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    test_type: Mapped[str] = mapped_column(String(30), nullable=False)
    input_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    expected_output_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    expected_schema_valid: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    assertions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Execution history
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    sandbox_job_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_result: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    last_output_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pass_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
