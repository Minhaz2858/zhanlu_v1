"""Agent run step model — per-iteration checkpoint snapshots.

Each LLM call or tool call within an AgentRun produces one row.
When ``AGENT_CHECKPOINTING_ENABLED`` is True, the CheckpointSink
persists these during execution so crashed queued runs can resume
from the last checkpoint.
"""

from sqlalchemy import Column, String, Integer, Text, DateTime
from app.models.base import TimestampedBase


class AgentRunStep(TimestampedBase):
    __tablename__ = "agent_run_steps"

    step_id = Column(String(32), unique=True, nullable=False, index=True)
    run_id = Column(String(32), nullable=False, index=True)

    # llm_call | tool_call | synthesis
    step_type = Column(String(20), nullable=False)

    step_index = Column(Integer, nullable=False)

    # JSON-serialised messages list, truncated to 4 KB so the
    # snapshot stays cheap.  Longer conversations are summarised by
    # keeping only the last 6 messages.
    messages_snapshot = Column(Text, nullable=True)

    # Only populated for tool_call steps; null for llm_call/synthesis.
    tool_name = Column(String(255), nullable=True)
    tool_args = Column(Text, nullable=True)          # JSON

    # First 4 KB of the LLM response or tool result — for observability.
    result_preview = Column(Text, nullable=True)

    iteration = Column(Integer, default=0)
    duration_ms = Column(Integer, nullable=True)

    # P0-3 observability: token usage + outcome per step (2026-08-27).
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    status = Column(String(20), nullable=True)  # ok | error | cancelled
    error = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)

    # P0-3 — explicit checkpoint marker.  When set, this step is a
    # resumption point: the harness restores from this row's
    # messages_snapshot on resume.  ``None`` means "regular step, not
    # a checkpoint boundary".
    is_checkpoint = Column(String(1), nullable=True, default=None)  # "Y" | None
    checkpoint = Column(Text, nullable=True)  # JSON: tool_input + tool_output snapshot
