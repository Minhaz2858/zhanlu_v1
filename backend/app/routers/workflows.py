"""Workflows router — flow-mode agent CRUD + validate / run / publish / runs.

Workflow agents are ordinary AgentApp rows with ``flow_mode=true`` and the
graph JSON in ``flow``. This router adds the canvas-specific surface on top
of the generic entity CRUD (which still owns AgentApp row management).

All endpoints are user-scoped (AgentApp is a USER_SCOPED_ENTITY): non-admins
only see/edit their own workflow agents.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required
from app.models.agent_app import AgentApp
from app.models.user import User
from app.models.workflow_run import WorkflowRun, WorkflowRunStep
from app.services.workflow_runtime import run_workflow_agent
from app.services.workflow_validation import validate_flow

router = APIRouter(prefix="/apps/{app_id}/workflows", tags=["workflows"])


def _get_owned_workflow(db: Session, app_id: str, workflow_id: str, user: User) -> AgentApp:
    q = db.query(AgentApp).filter(
        AgentApp.id == workflow_id,
        AgentApp.app_id == app_id,
        AgentApp.flow_mode.is_(True),
        AgentApp.is_deleted.is_(False),
    )
    if user.role != "admin":
        q = q.filter(AgentApp.created_by_id == user.id)
    agent = q.first()
    if agent is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return agent


class RunBody(BaseModel):
    input: str = ""


@router.get("")
def list_workflows(
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    q = db.query(AgentApp).filter(
        AgentApp.app_id == app_id,
        AgentApp.flow_mode.is_(True),
        AgentApp.is_deleted.is_(False),
    )
    if user.role != "admin":
        q = q.filter(AgentApp.created_by_id == user.id)
    return [a.to_dict() for a in q.order_by(AgentApp.updated_date.desc()).all()]


@router.post("")
def create_workflow(
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    agent = AgentApp(
        name="Untitled Workflow",
        description="",
        flow_mode=True,
        flow={"version": 1, "nodes": [], "edges": []},
        status="draft",
        app_id=app_id,
        org_id="default-org",
        created_by_id=user.id,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent.to_dict()


@router.get("/{workflow_id}")
def get_workflow(
    workflow_id: str,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    return _get_owned_workflow(db, app_id, workflow_id, user).to_dict()


class SaveBody(BaseModel):
    name: str | None = None
    description: str | None = None
    flow: dict


@router.put("/{workflow_id}")
def save_workflow(
    workflow_id: str,
    body: SaveBody,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    errors = validate_flow(body.flow)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    agent.flow = body.flow
    if body.name:
        agent.name = body.name
    if body.description is not None:
        agent.description = body.description
    db.commit()
    db.refresh(agent)
    return agent.to_dict()


@router.post("/{workflow_id}/validate")
def validate_workflow(
    workflow_id: str,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    errors = validate_flow(agent.flow or {})
    return {"valid": not errors, "errors": errors}


@router.post("/{workflow_id}/run")
def run_workflow(
    workflow_id: str,
    body: RunBody,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    events: list[dict] = []
    result = run_workflow_agent(
        db, agent.org_id, agent.app_id, agent, body.input,
        created_by_id=user.id, on_event=events.append,
    )
    return {**result, "events": events}


@router.get("/{workflow_id}/runs")
def list_runs(
    workflow_id: str,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    runs = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.agent_app_id == agent.id, WorkflowRun.is_deleted.is_(False))
        .order_by(WorkflowRun.created_date.desc())
        .limit(50)
        .all()
    )
    out = []
    for run in runs:
        steps = (
            db.query(WorkflowRunStep)
            .filter(WorkflowRunStep.run_id == run.id, WorkflowRunStep.is_deleted.is_(False))
            .order_by(WorkflowRunStep.created_date.asc())
            .all()
        )
        out.append({
            **run.to_dict(),
            "steps": [s.to_dict() for s in steps],
        })
    return out


@router.post("/{workflow_id}/publish")
def publish_workflow(
    workflow_id: str,
    app_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    agent = _get_owned_workflow(db, app_id, workflow_id, user)
    errors = validate_flow(agent.flow or {})
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    agent.status = "published"
    db.commit()
    db.refresh(agent)
    return agent.to_dict()
