"""AgentInvocation — captures every agent invocation with tracing and cost.

Equivalent to a "request/response trace" — tracks who invoked what,
how long it took, what tokens were consumed, and the result.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Float, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

AGENT_INVOCATION_TRIGGERS = ["user", "scheduled", "webhook", "pipeline", "retry"]
AGENT_INVOCATION_STATUSES = ["pending", "running", "completed", "failed", "timeout", "cancelled"]


class AgentInvocation(TimestampedBase):
    """A single agent invocation — full request/response trace record."""

    __tablename__ = "agent_invocations"

    # References
    agent_app_id: Mapped[str] = mapped_column(String(36), nullable=False)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    execution_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Invocation type
    invocation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    trigger: Mapped[str] = mapped_column(String(30), default="user", nullable=False)
    input_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Result
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    output_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    assistant_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Timing
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Cost
    token_usage: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    cost_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Model + workload (observability)
    model_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tool_call_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Tracing
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    span_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
