"""AgentTestCase — test cases for agent behavior verification.

Defines a test with input, expected output, and assertions that can
be re-run periodically or on changes.  Tracks pass/fail history and
run counts.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

AGENT_TEST_TYPES = ["unit", "integration", "acceptance", "regression", "performance"]
AGENT_TEST_RESULTS = ["pending", "pass", "fail", "error", "timeout"]


class AgentTestCase(TimestampedBase):
    """A test case that verifies agent behavior with known inputs/outputs."""

    __tablename__ = "agent_test_cases"

    # References
    agent_app_id: Mapped[str] = mapped_column(String(36), nullable=False)
    skill_profile_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Test spec
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    test_type: Mapped[str] = mapped_column(String(30), nullable=False)
    input_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    expected_output_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    expected_behavior: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assertions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Execution history
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_result: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    last_output_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pass_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
