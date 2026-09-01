"""Sandbox service — job creation, enqueue, status tracking, and event recording.

This service is the backend-facing API for sandbox execution.  It creates
SandboxJob records, enqueues them for the worker, and records events/commands
as they stream back from the worker.

The actual Docker container management is handled by the standalone
sandbox-worker service (sandbox_worker/main.py) which picks up jobs from
Redis and executes them.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.sandbox_job import SandboxJob, SandboxJobEvent, SandboxCommand, SANDBOX_JOB_STATUSES

logger = logging.getLogger(__name__)


class SandboxService:
    """Service for managing sandbox jobs and their execution timeline."""

    def __init__(self, db: Session):
        self.db = db

    # --- Job lifecycle ---

    def create_job(
        self,
        skill_name: str,
        artifact_id: Optional[str] = None,
        artifact_version_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        skill_version: Optional[str] = None,
        input_package: Optional[dict] = None,
        output_spec: Optional[dict] = None,
        timeout_seconds: int = 120,
        image_name: Optional[str] = None,
        org_id: str = "default-org",
        app_id: str = "default-app",
    ) -> SandboxJob:
        """Create a new sandbox job in 'queued' status and enqueue it."""
        job = SandboxJob(
            id=str(uuid4()),
            skill_name=skill_name,
            skill_version=skill_version,
            artifact_id=artifact_id,
            artifact_version_id=artifact_version_id,
            conversation_id=conversation_id,
            execution_id=execution_id,
            input_package=input_package,
            output_spec=output_spec,
            status="queued",
            timeout_seconds=timeout_seconds,
            image_name=image_name,
            org_id=org_id,
            app_id=app_id,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

        # Record initial event
        self.record_event(job.id, "job_queued", f"Sandbox job queued for skill '{skill_name}'")

        # Enqueue to Redis if available
        self._enqueue(job)

        logger.info("Created sandbox job %s for skill %s", job.id, skill_name)
        return job

    def _enqueue(self, job: SandboxJob):
        """Enqueue job to Redis for the sandbox worker to pick up."""
        try:
            from app.database import get_redis
            redis = get_redis()
            if redis:
                import json
                redis.rpush("sandbox:queue", json.dumps({
                    "job_id": job.id,
                    "skill_name": job.skill_name,
                    "image_name": job.image_name,
                    "timeout_seconds": job.timeout_seconds,
                }))
                logger.info("Enqueued sandbox job %s to Redis", job.id)
            else:
                logger.info("Redis not available — sandbox job %s will use fallback executor", job.id)
        except Exception as e:
            logger.warning("Failed to enqueue to Redis: %s — using fallback", e)

    def get_job(self, job_id: str) -> Optional[SandboxJob]:
        """Get a sandbox job by ID."""
        return self.db.query(SandboxJob).filter(SandboxJob.id == job_id).first()

    def update_job_status(
        self,
        job_id: str,
        status: str,
        container_id: Optional[str] = None,
        error_message: Optional[str] = None,
        exit_code: Optional[int] = None,
        output_artifact_ids: Optional[list] = None,
    ) -> Optional[SandboxJob]:
        """Update a job's status and execution details."""
        if status not in SANDBOX_JOB_STATUSES:
            raise ValueError(f"Invalid status '{status}'")

        job = self.get_job(job_id)
        if not job:
            return None

        job.status = status
        if container_id:
            job.container_id = container_id
        if error_message is not None:
            job.error_message = error_message
        if exit_code is not None:
            job.exit_code = exit_code
        if output_artifact_ids is not None:
            job.output_artifact_ids = output_artifact_ids

        if status == "running" and not job.started_at:
            job.started_at = datetime.now(timezone.utc)
        if status in ("completed", "failed", "timeout", "cancelled"):
            job.completed_at = datetime.now(timezone.utc)

        self.db.commit()
        self.db.refresh(job)
        logger.info("Sandbox job %s → %s", job_id, status)
        return job

    # --- Event recording ---

    def record_event(
        self,
        job_id: str,
        event_type: str,
        message: Optional[str] = None,
        data_json: Optional[dict] = None,
    ) -> SandboxJobEvent:
        """Record a timeline event for a sandbox job."""
        # Get next sequence number
        latest = (
            self.db.query(SandboxJobEvent)
            .filter(SandboxJobEvent.job_id == job_id)
            .order_by(SandboxJobEvent.seq.desc())
            .first()
        )
        next_seq = (latest.seq + 1) if latest else 0

        event = SandboxJobEvent(
            id=str(uuid4()),
            job_id=job_id,
            seq=next_seq,
            event_type=event_type,
            message=message,
            data_json=data_json,
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)

        # Publish to Redis for SSE fanout
        self._publish_event(job_id, event)

        return event

    def _publish_event(self, job_id: str, event: SandboxJobEvent):
        """Publish event to Redis for real-time SSE fanout."""
        try:
            from app.database import get_redis
            redis = get_redis()
            if redis:
                import json
                redis.publish(f"sandbox:events:{job_id}", json.dumps({
                    "seq": event.seq,
                    "event_type": event.event_type,
                    "message": event.message,
                    "data": event.data_json,
                    "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                }))
        except Exception:
            pass  # Non-critical — events are also queryable via API

    def get_events(self, job_id: str, after_seq: int = -1) -> list[SandboxJobEvent]:
        """Get events for a job, optionally after a sequence number (for polling)."""
        query = (
            self.db.query(SandboxJobEvent)
            .filter(SandboxJobEvent.job_id == job_id, SandboxJobEvent.seq > after_seq)
            .order_by(SandboxJobEvent.seq)
        )
        return query.all()

    # --- Command recording ---

    def record_command(
        self,
        job_id: str,
        command: str,
        cwd: Optional[str] = None,
        exit_code: Optional[int] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> SandboxCommand:
        """Record a command executed inside the sandbox."""
        latest = (
            self.db.query(SandboxCommand)
            .filter(SandboxCommand.job_id == job_id)
            .order_by(SandboxCommand.seq.desc())
            .first()
        )
        next_seq = (latest.seq + 1) if latest else 0

        cmd = SandboxCommand(
            id=str(uuid4()),
            job_id=job_id,
            seq=next_seq,
            command=command,
            cwd=cwd,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        self.db.add(cmd)
        self.db.commit()
        self.db.refresh(cmd)
        return cmd

    def get_commands(self, job_id: str) -> list[SandboxCommand]:
        """Get all commands executed in a sandbox job."""
        return (
            self.db.query(SandboxCommand)
            .filter(SandboxCommand.job_id == job_id)
            .order_by(SandboxCommand.seq)
            .all()
        )

    # --- Job listing ---

    def list_jobs(
        self,
        conversation_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[SandboxJob]:
        """List sandbox jobs with optional filters."""
        query = self.db.query(SandboxJob).filter(SandboxJob.is_deleted == False)
        if conversation_id:
            query = query.filter(SandboxJob.conversation_id == conversation_id)
        if artifact_id:
            query = query.filter(SandboxJob.artifact_id == artifact_id)
        if status:
            query = query.filter(SandboxJob.status == status)
        return query.order_by(SandboxJob.created_date.desc()).limit(limit).all()
