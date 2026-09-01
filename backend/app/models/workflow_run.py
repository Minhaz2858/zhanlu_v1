"""WorkflowRun + WorkflowRunStep — execution records for flow-mode agents.

One WorkflowRun per execution of a flow_mode AgentApp (chat trigger or
editor test run). One WorkflowRunStep per node executed — used by the
run drawer (editor) and the chat step card (streaming events).
"""

from sqlalchemy import String, Text, JSON, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class WorkflowRun(TimestampedBase):
    __tablename__ = "workflow_runs"

    # The flow_mode AgentApp this run executed.
    agent_app_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # chat | test — where the run came from.
    trigger: Mapped[str] = mapped_column(String(50), nullable=False, default="chat")
    # running | completed | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)


class WorkflowRunStep(TimestampedBase):
    __tablename__ = "workflow_run_steps"

    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # pending | running | completed | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
