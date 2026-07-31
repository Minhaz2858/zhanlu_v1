"""Synexia cognitive core models — Execution, Plan, PlanNode, ObservationRecord, ContextManifest.

The Synexia FSM replaces the raw LLM tool-calling loop with a governed
cognitive pipeline:

    INIT → GOAL → CONTEXT → PLAN → GATE → ACT → OBSERVE → VERIFY → FINALIZE

Every request creates an Execution record that tracks the FSM state,
the Plan (DAG of nodes), observations from each step, and the final
confidence score.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Float, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TimestampedBase


# FSM states
FSM_STATES = [
    "init", "goal", "context", "plan", "gate", "act", "observe", "verify", "finalize",
    "quality_eval",
    "done", "fail",
]

# Execution modes
EXECUTION_MODES = ["dynamic", "frozen"]  # frozen = replay a previous plan

# Plan node statuses
NODE_STATUSES = ["pending", "approved", "running", "completed", "failed", "skipped"]


class Execution(TimestampedBase):
    """A single execution of the Synexia FSM — the cognitive pipeline run.

    Created when a user sends a message.  Tracks the FSM state machine,
    links to the conversation, and records the final result.
    """

    __tablename__ = "executions"

    conversation_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    agent_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)

    # FSM state
    current_state: Mapped[str] = mapped_column(String(20), default="init", nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="dynamic", nullable=False)

    # Task spec (parsed from user message)
    task_spec: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Context manifest
    context_manifest: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Policy
    policy_decision: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Result
    assistant_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    artifact_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence_factors: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Error
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timing
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    plan: Mapped[Optional["Plan"]] = relationship(back_populates="execution", uselist=False, cascade="all, delete-orphan")
    observations: Mapped[list["ObservationRecord"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan", order_by="ObservationRecord.seq"
    )


class Plan(TimestampedBase):
    """A PlanDAG — the execution plan as a directed acyclic graph of nodes.

    The plan is visible, editable, and versioned.  The user can approve,
    modify, or reject it before execution begins (at the GATE state).
    """

    __tablename__ = "plans"

    execution_id: Mapped[str] = mapped_column(String(36), ForeignKey("executions.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)  # draft | approved | rejected | executing | completed
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_acyclic: Mapped[bool] = mapped_column(default=True, nullable=False)

    execution: Mapped["Execution"] = relationship(back_populates="plan")
    nodes: Mapped[list["PlanNode"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PlanNode.seq"
    )


class PlanNode(TimestampedBase):
    """A single node in the PlanDAG — one step of the execution plan.

    Each node specifies what to do (skill/tool/agent), with what inputs,
    and what the expected output is.  Dependencies on other nodes are
    recorded for DAG ordering.
    """

    __tablename__ = "plan_nodes"

    plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("plans.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    node_type: Mapped[str] = mapped_column(String(30), nullable=False)  # skill | tool | agent | sandbox | nl2sql
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Dependencies (list of node seq numbers this depends on)
    dependencies: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Inputs/outputs
    inputs: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    expected_output: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    output_artifact_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Execution
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    risk_tier: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # low | medium | high
    requires_confirmation: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Result
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    plan: Mapped["Plan"] = relationship(back_populates="nodes")


class ObservationRecord(TimestampedBase):
    """A structured observation from executing a plan node.

    Instead of the LLM's raw tool-call output, the FSM records structured
    observations: what was requested, what was returned, whether it succeeded,
    and any artifacts produced.
    """

    __tablename__ = "observation_records"

    execution_id: Mapped[str] = mapped_column(String(36), ForeignKey("executions.id"), nullable=False, index=True)
    plan_node_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("plan_nodes.id"), nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_type: Mapped[str] = mapped_column(String(30), nullable=False)  # tool_call | sandbox | nl2sql | artifact | error
    tool_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    request_args: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(default=True, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    artifact_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    execution: Mapped["Execution"] = relationship(back_populates="observations")
