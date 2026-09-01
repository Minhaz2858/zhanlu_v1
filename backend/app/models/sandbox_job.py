"""Sandbox execution models — isolated Docker job tracking and event timeline.

The sandbox system provides Claude-Code-like live execution visibility:
every command, file creation, and validation event is streamed to the chat
as a timeline the user can watch in real-time.

Key safety rules (from architecture docs):
* Sandbox NEVER receives database credentials — only DataSnapshot JSON/CSV
* Input package (approved skill + DataSnapshot + template) is mounted read-only
* Output folder is writable
* Container is destroyed after execution; outputs stored in PostgreSQL
* Container runs with --network none --read-only --memory 1g --cap-drop ALL
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, Integer, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TimestampedBase


# Valid sandbox job statuses
SANDBOX_JOB_STATUSES = [
    "queued",       # Created, waiting for worker pickup
    "running",      # Worker picked up, container started
    "completed",    # Finished successfully
    "failed",       # Finished with error
    "timeout",      # Exceeded time limit
    "cancelled",    # User cancelled
]


class SandboxJob(TimestampedBase):
    """A sandbox execution job — runs a skill in an isolated Docker container.

    Created by the backend when an artifact-generating skill needs to execute.
    Picked up by the standalone sandbox-worker service via Redis queue.
    """

    __tablename__ = "sandbox_jobs"

    # Links
    artifact_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    artifact_version_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    execution_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)

    # Job spec
    skill_name: Mapped[str] = mapped_column(String(100), nullable=False)
    skill_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    input_package: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # skill config + snapshot refs + template
    output_spec: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # expected output types

    # Execution state
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    container_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    image_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Timing
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120, nullable=False)

    # Results
    output_artifact_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Resource usage
    memory_used_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_time_seconds: Mapped[Optional[float]] = mapped_column(Integer, nullable=True)

    events: Mapped[list["SandboxJobEvent"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="SandboxJobEvent.seq"
    )
    commands: Mapped[list["SandboxCommand"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="SandboxCommand.seq"
    )


class SandboxJobEvent(TimestampedBase):
    """A timeline event during sandbox execution (streamed to frontend via SSE).

    Event types: job_queued, job_started, command_started, stdout, stderr,
    file_created, file_modified, validation_started, validation_passed,
    validation_failed, job_completed, job_failed, job_timeout
    """

    __tablename__ = "sandbox_job_events"

    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("sandbox_jobs.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # Sequence number for ordering
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    data_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    job: Mapped["SandboxJob"] = relationship(back_populates="events")


class SandboxCommand(TimestampedBase):
    """A command executed inside the sandbox container.

    Each command is recorded with its stdout/stderr for audit and replay.
    """

    __tablename__ = "sandbox_commands"

    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("sandbox_jobs.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    cwd: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stdout: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stderr: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    job: Mapped["SandboxJob"] = relationship(back_populates="commands")
