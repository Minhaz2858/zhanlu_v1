"""Executions router — execution state, plan, and confirmation APIs.

Provides:
* GET /api/executions/{id} — get execution state + plan + observations
* POST /api/executions — create and run a new execution (FSM pipeline)
* GET /api/executions/{id}/plan — get the execution plan (PlanDAG)
* PATCH /api/executions/{id}/plan/approve — approve the plan (pass GATE)
* PATCH /api/executions/{id}/plan/reject — reject the plan
* GET /api/executions/{id}/observations — get observation records

Security: every endpoint requires an authenticated user.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_db, get_current_user_required
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["executions"])


class CreateExecutionRequest(BaseModel):
    conversation_id: Optional[str] = None
    agent_name: str = "general_assistant"
    user_message: str
    user_id: Optional[str] = None
    mode: str = "dynamic"


@router.post("/executions")
def create_execution(
    req: CreateExecutionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Create and run a new Synexia FSM execution."""
    if not settings.SYNEXIA_FSM_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Synexia FSM is not enabled. Set SYNEXIA_FSM_ENABLED=true to enable.",
        )

    from app.services.synexia.fsm import SynexiaFSM, ExecutionRequest

    fsm = SynexiaFSM(db)
    result = fsm.run(ExecutionRequest(
        conversation_id=req.conversation_id,
        agent_name=req.agent_name,
        user_message=req.user_message,
        user_id=user.id,
        mode=req.mode,
    ))

    return {
        "execution_id": result.execution_id,
        "state": result.state,
        "assistant_content": result.assistant_content,
        "tool_calls": result.tool_calls,
        "artifact_ids": result.artifact_ids,
        "confidence": result.confidence,
        "confidence_factors": result.confidence_factors,
        "plan_summary": result.plan_summary,
        "report_card_payload": result.report_card_payload,
        "file_exports": result.file_exports,
        "export_artifact_id": result.export_artifact_id,
    }


@router.get("/executions/{execution_id}")
def get_execution(
    execution_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Get execution state, plan, and observations."""
    from app.models.execution import Execution

    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    result = execution.to_dict()
    result["observations"] = [o.to_dict() for o in execution.observations]

    if execution.plan:
        plan_data = execution.plan.to_dict()
        plan_data["nodes"] = [n.to_dict() for n in execution.plan.nodes]
        result["plan"] = plan_data

    return result


@router.get("/executions/{execution_id}/plan")
def get_plan(
    execution_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Get the execution plan (PlanDAG)."""
    from app.models.execution import Execution

    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if not execution.plan:
        raise HTTPException(status_code=404, detail="No plan for this execution")

    plan = execution.plan.to_dict()
    plan["nodes"] = [n.to_dict() for n in execution.plan.nodes]
    return plan


@router.patch("/executions/{execution_id}/plan/approve")
def approve_plan(
    execution_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Approve the execution plan (pass the GATE state)."""
    from app.models.execution import Execution

    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if not execution.plan:
        raise HTTPException(status_code=404, detail="No plan for this execution")

    execution.plan.status = "approved"
    db.commit()

    return {"execution_id": execution_id, "plan_status": "approved"}


@router.patch("/executions/{execution_id}/plan/reject")
def reject_plan(
    execution_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Reject the execution plan."""
    from app.models.execution import Execution

    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if not execution.plan:
        raise HTTPException(status_code=404, detail="No plan for this execution")

    execution.plan.status = "rejected"
    execution.current_state = "fail"
    db.commit()

    return {"execution_id": execution_id, "plan_status": "rejected"}


@router.get("/executions/{execution_id}/observations")
def get_observations(
    execution_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Get observation records for an execution."""
    from app.models.execution import Execution, ObservationRecord

    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    return [o.to_dict() for o in execution.observations]
