"""SkillRun — execution history record for a single skill invocation.

Every time a skill is invoked (through an agent, manually, or as part
of a pipeline), a SkillRun record captures the input, output, timing,
cost, and error if any.  Supports retry via attempt_number.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Float, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

SKILL_RUN_STATUSES = ["running", "completed", "failed", "timeout", "cancelled", "retrying"]


class SkillRun(TimestampedBase):
    """A single skill execution — input, output, timing, cost, retries."""

    __tablename__ = "skill_runs"

    # References
    skill_profile_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    agent_skill_binding_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    execution_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Execution
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    input_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    output_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    artifacts_produced: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Timing
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Cost
    token_usage: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    cost_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Retry
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    parent_run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
