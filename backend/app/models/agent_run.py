"""Agent run record model — persisted execution audit trail.

Each delegated/queued execution gets one row.  The harness writes a start
event on creation and a finish event (result + error) on completion.
Tool-call arrays and iteration counts are snapshotted for observability.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, Text, DateTime
from app.models.base import TimestampedBase


class AgentRun(TimestampedBase):
    __tablename__ = "agent_runs"

    run_id = Column(String(32), unique=True, nullable=False, index=True)
    agent_name = Column(String(255), nullable=False, index=True)
    task = Column(Text, nullable=False)
    status = Column(
        String(20), default="queued", nullable=False, index=True
    )  # queued | running | completed | failed
    mode = Column(
        String(10), default="inline", nullable=False
    )  # inline | queued

    # --- result payloads (JSON-text columns) ---
    result = Column(Text, nullable=True)          # {"answer": "...", "success": bool}
    tool_calls = Column(Text, nullable=True)       # JSON array of tool-call dicts
    tool_call_count = Column(Integer, default=0)
    iterations = Column(Integer, default=0)

    # --- linkage ---
    parent_run_id = Column(String(32), nullable=True, index=True)
    caller_context = Column(Text, nullable=True)   # JSON: org_id, app_id, user_id, ...

    # --- error / timing ---
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
