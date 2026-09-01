"""Sandbox router — job status, event stream (SSE), and command log APIs.

Provides:
* POST /api/sandbox/jobs — create a new sandbox job
* GET /api/sandbox/jobs/{id} — get job status + details
* GET /api/sandbox/jobs/{id}/events — get timeline events (with polling via after_seq)
* GET /api/sandbox/jobs/{id}/events/stream — SSE stream for real-time updates
* GET /api/sandbox/jobs/{id}/commands — get command log
* GET /api/sandbox/jobs — list jobs (with filters)
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deps import get_current_user_required
from app.services.sandbox.sandbox_service import SandboxService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sandbox"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class CreateJobRequest(BaseModel):
    skill_name: str
    skill_version: Optional[str] = None
    artifact_id: Optional[str] = None
    artifact_version_id: Optional[str] = None
    conversation_id: Optional[str] = None
    execution_id: Optional[str] = None
    input_package: Optional[dict] = None
    output_spec: Optional[dict] = None
    timeout_seconds: int = 120
    image_name: Optional[str] = None


@router.post("/sandbox/jobs")
def create_job(req: CreateJobRequest, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    """Create a new sandbox job (queued for worker pickup)."""
    service = SandboxService(db)
    job = service.create_job(
        skill_name=req.skill_name,
        skill_version=req.skill_version,
        artifact_id=req.artifact_id,
        artifact_version_id=req.artifact_version_id,
        conversation_id=req.conversation_id,
        execution_id=req.execution_id,
        input_package=req.input_package,
        output_spec=req.output_spec,
        timeout_seconds=req.timeout_seconds,
        image_name=req.image_name,
    )
    return job.to_dict()


@router.get("/sandbox/jobs")
def list_jobs(
    conversation_id: Optional[str] = Query(None),
    artifact_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    """List sandbox jobs with optional filters."""
    service = SandboxService(db)
    jobs = service.list_jobs(
        conversation_id=conversation_id,
        artifact_id=artifact_id,
        status=status,
        limit=limit,
    )
    return [j.to_dict() for j in jobs]


@router.get("/sandbox/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    """Get sandbox job status and details."""
    service = SandboxService(db)
    job = service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    result = job.to_dict()
    result["events"] = [
        {
            "seq": e.seq,
            "event_type": e.event_type,
            "message": e.message,
            "data": e.data_json,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
        }
        for e in service.get_events(job_id)
    ]
    result["commands"] = [
        {
            "seq": c.seq,
            "command": c.command,
            "exit_code": c.exit_code,
            "stdout": c.stdout,
            "stderr": c.stderr,
            "duration_ms": c.duration_ms,
        }
        for c in service.get_commands(job_id)
    ]
    return result


@router.get("/sandbox/jobs/{job_id}/events")
def get_events(
    job_id: str,
    after_seq: int = Query(-1, description="Only return events after this sequence number"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Get timeline events for a job (polling endpoint)."""
    service = SandboxService(db)
    events = service.get_events(job_id, after_seq=after_seq)
    return [
        {
            "seq": e.seq,
            "event_type": e.event_type,
            "message": e.message,
            "data": e.data_json,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
        }
        for e in events
    ]


@router.get("/sandbox/jobs/{job_id}/events/stream")
async def stream_events(job_id: str, user=Depends(get_current_user_required)):
    """SSE stream for real-time sandbox job events.

    Falls back to polling when Redis is not available — the stream
    queries the database every 1 second for new events.
    """
    import time
    from app.database import SessionLocal

    async def event_generator():
        last_seq = -1
        idle_count = 0
        max_idle = 300  # 5 minutes of no events → close stream

        while idle_count < max_idle:
            db = SessionLocal()
            try:
                service = SandboxService(db)
                job = service.get_job(job_id)

                # Check if job is done
                if job and job.status in ("completed", "failed", "timeout", "cancelled"):
                    events = service.get_events(job_id, after_seq=last_seq)
                    for event in events:
                        last_seq = event.seq
                        data = {
                            "seq": event.seq,
                            "event_type": event.event_type,
                            "message": event.message,
                            "data": event.data_json,
                            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                        }
                        yield f"data: {json.dumps(data)}\n\n"

                    # Send final close event
                    yield f"data: {json.dumps({'event_type': 'stream_end', 'job_status': job.status})}\n\n"
                    break

                # Send new events
                events = service.get_events(job_id, after_seq=last_seq)
                if events:
                    idle_count = 0
                    for event in events:
                        last_seq = event.seq
                        data = {
                            "seq": event.seq,
                            "event_type": event.event_type,
                            "message": event.message,
                            "data": event.data_json,
                            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                        }
                        yield f"data: {json.dumps(data)}\n\n"
                else:
                    idle_count += 1
            finally:
                db.close()

            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sandbox/jobs/{job_id}/commands")
def get_commands(job_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_required)):
    """Get the command log for a sandbox job."""
    service = SandboxService(db)
    commands = service.get_commands(job_id)
    return [
        {
            "seq": c.seq,
            "command": c.command,
            "exit_code": c.exit_code,
            "stdout": c.stdout,
            "stderr": c.stderr,
            "duration_ms": c.duration_ms,
        }
        for c in commands
    ]
