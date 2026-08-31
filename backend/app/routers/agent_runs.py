"""P0-3 — Agent Harness run-trail router.

Three read/endpoints over the AgentRun / AgentRunStep audit trail that
the harness already records (gated by ``AGENT_HARNESS_ENABLED``):

  * ``GET  /api/agent-runs/{run_id}``            — run + steps
  * ``POST /api/agent-runs/{run_id}/resume``     — fork a child run, mark
                                                   the parent failed
  * ``GET  /api/agent-runs/conversations/{cid}``  — list runs whose
                                                   ``caller_context`` JSON
                                                   has ``conversation_id``

Auth: same soft-auth pattern as ``/api/agents/conversations`` (the
internal audit surface is the same as the conversation surface).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_optional
from app.services.harness.run_service import AgentRunService
from app.models.user import User

router = APIRouter(prefix="/agent-runs", tags=["agent-runs"])


# ── Pydantic response shapes ──────────────────────────────────────────────

class StepOut(BaseModel):
    step_id: str
    run_id: str
    step_type: str
    step_index: int
    tool_name: Optional[str] = None
    iteration: int
    duration_ms: int
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    status: Optional[str] = None
    error: Optional[str] = None
    retry_count: Optional[int] = None
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class RunOut(BaseModel):
    run_id: str
    agent_name: str
    task: str
    status: str
    mode: str
    iterations: int
    tool_call_count: int
    parent_run_id: Optional[str] = None
    created_at: Optional[str] = None
    finished_at: Optional[str] = None
    steps: list[StepOut] = []


class RunSummary(BaseModel):
    run_id: str
    agent_name: str
    task: str
    status: str
    iterations: int
    tool_call_count: int
    parent_run_id: Optional[str] = None
    created_at: Optional[str] = None


class ResumeResponse(BaseModel):
    new_run_id: str
    parent_run_id: str
    status: str


# ── Helpers ───────────────────────────────────────────────────────────────

def _to_run_out(run, steps=None) -> RunOut:
    return RunOut(
        run_id=run.run_id,
        agent_name=run.agent_name,
        task=run.task,
        status=run.status,
        mode=run.mode,
        iterations=run.iterations,
        tool_call_count=run.tool_call_count,
        parent_run_id=run.parent_run_id,
        created_at=run.created_date.isoformat() if run.created_date else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        steps=[_to_step_out(s) for s in (steps or [])],
    )


def _to_step_out(step) -> StepOut:
    return StepOut(
        step_id=step.step_id,
        run_id=step.run_id,
        step_type=step.step_type,
        step_index=step.step_index,
        tool_name=step.tool_name,
        iteration=step.iteration,
        duration_ms=step.duration_ms or 0,
        prompt_tokens=getattr(step, "prompt_tokens", None),
        completion_tokens=getattr(step, "completion_tokens", None),
        total_tokens=getattr(step, "total_tokens", None),
        status=getattr(step, "status", None),
        error=getattr(step, "error", None),
        retry_count=getattr(step, "retry_count", None),
        created_at=step.created_date.isoformat() if step.created_date else None,
    )


def _to_run_summary(run) -> RunSummary:
    return RunSummary(
        run_id=run.run_id,
        agent_name=run.agent_name,
        task=run.task,
        status=run.status,
        iterations=run.iterations,
        tool_call_count=run.tool_call_count,
        parent_run_id=run.parent_run_id,
        created_at=run.created_date.isoformat() if run.created_date else None,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("/{run_id}", response_model=RunOut)
def get_run(
    run_id: str,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Return run status + ordered step list.  404 if not found."""
    from app.models.agent_run import AgentRun
    from app.models.agent_run_step import AgentRunStep
    run = db.query(AgentRun).filter(
        AgentRun.run_id == run_id, AgentRun.is_deleted == False
    ).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    steps = db.query(AgentRunStep).filter(
        AgentRunStep.run_id == run_id, AgentRunStep.is_deleted == False
    ).order_by(AgentRunStep.step_index.asc()).all()
    return _to_run_out(run, steps)


@router.post("/{run_id}/resume", response_model=ResumeResponse)
def resume_run(
    run_id: str,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Fork a child run from a failed/crashed parent and mark the parent
    as 'failed' (so it cannot be resumed twice).  409 if the run is in a
    terminal-completed state.  404 if not found.
    """
    from app.models.agent_run import AgentRun
    svc = AgentRunService()
    parent = db.query(AgentRun).filter(
        AgentRun.run_id == run_id, AgentRun.is_deleted == False
    ).first()
    if not parent:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    if parent.status == 'completed':
        raise HTTPException(
            status_code=409,
            detail=f"run {run_id} is already completed; nothing to resume",
        )
    new_run = svc.create_run_record(
        agent_name=parent.agent_name,
        task=parent.task,
        mode=parent.mode or 'inline',
        parent_run_id=parent.run_id,
        caller_context=parent.caller_context,
        db=db,
    )
    # Mark the old run as 'failed' so we don't loop on resume.
    svc.finalize_run(parent.run_id, status='failed', db=db)
    return ResumeResponse(
        new_run_id=new_run.run_id,
        parent_run_id=parent.run_id,
        status=new_run.status,
    )


@router.get("/conversations/{conversation_id}", response_model=list[RunSummary])
def list_runs_for_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Return all runs whose caller_context records this conversation_id,
    newest first.  Empty list if no runs exist for the conversation.
    """
    import json as _json
    from app.models.agent_run import AgentRun
    rows = db.query(AgentRun).filter(
        AgentRun.is_deleted == False
    ).order_by(AgentRun.created_date.desc()).all()
    out = []
    for r in rows:
        if not r.caller_context:
            continue
        try:
            ctx = _json.loads(r.caller_context) if isinstance(r.caller_context, str) else r.caller_context
        except Exception:
            continue
        if isinstance(ctx, dict) and ctx.get('conversation_id') == conversation_id:
            out.append(_to_run_summary(r))
    return out
