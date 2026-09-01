"""Governance router — policy decisions, approvals, cost, and audit APIs.

Provides:
* GET /api/governance/policy-decisions — list policy decisions
* GET /api/governance/approvals — list approval requests
* POST /api/governance/approvals — create approval request
* POST /api/governance/approvals/{id}/approve — approve request
* POST /api/governance/approvals/{id}/reject — reject request
* GET /api/governance/cost — get cost summary
* GET /api/governance/cost/breakdown — get cost breakdown by type
* GET /api/governance/audit — list audit logs
* POST /api/governance/audit — record audit entry

Security: every endpoint requires an authenticated user (``get_current_user_required``).
Actor/reviewer identity is taken from the authenticated user — never trusted from
the request body — so audit trails and approval decisions cannot be forged.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["governance"])


# --- Schemas ---

class CreateApprovalRequest(BaseModel):
    action_type: str
    action_description: str
    risk_tier: str = "medium"
    execution_id: Optional[str] = None
    plan_node_id: Optional[str] = None
    artifact_id: Optional[str] = None
    context_json: Optional[dict] = None
    ttl_hours: int = 24


class ReviewApprovalRequest(BaseModel):
    notes: Optional[str] = None


class RejectApprovalRequest(BaseModel):
    reason: str


class RecordAuditRequest(BaseModel):
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    description: Optional[str] = None
    details_json: Optional[dict] = None
    execution_id: Optional[str] = None
    result: str = "success"


# --- Policy Decisions ---

@router.get("/governance/policy-decisions")
def list_policy_decisions(
    execution_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """List policy decisions for audit."""
    from app.models.governance import PolicyDecision
    query = db.query(PolicyDecision).filter(PolicyDecision.is_deleted == False)
    if execution_id:
        query = query.filter(PolicyDecision.execution_id == execution_id)
    decisions = query.order_by(PolicyDecision.created_date.desc()).limit(limit).all()
    return [d.to_dict() for d in decisions]


# --- Approvals ---

@router.get("/governance/approvals")
def list_approvals(
    status: Optional[str] = Query(None),
    execution_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """List approval requests."""
    from app.services.governance.approval_service import ApprovalService
    service = ApprovalService(db)
    requests = service.list_requests(status=status, execution_id=execution_id, limit=limit)
    return [r.to_dict() for r in requests]


@router.post("/governance/approvals")
def create_approval(
    req: CreateApprovalRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Create a new approval request."""
    from app.services.governance.approval_service import ApprovalService
    service = ApprovalService(db)
    request = service.create_request(
        action_type=req.action_type,
        action_description=req.action_description,
        risk_tier=req.risk_tier,
        execution_id=req.execution_id,
        plan_node_id=req.plan_node_id,
        artifact_id=req.artifact_id,
        context_json=req.context_json,
        ttl_hours=req.ttl_hours,
    )
    return request.to_dict()


@router.post("/governance/approvals/{request_id}/approve")
def approve_request(
    request_id: str,
    req: ReviewApprovalRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Approve a pending request.

    The reviewer identity is the authenticated user — never the request body.
    """
    from app.services.governance.approval_service import ApprovalService
    service = ApprovalService(db)
    reviewed_by = user.full_name or user.id
    try:
        request = service.approve(request_id, reviewed_by=reviewed_by, notes=req.notes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not request:
        raise HTTPException(status_code=404, detail="Request not found")
    return request.to_dict()


@router.post("/governance/approvals/{request_id}/reject")
def reject_request(
    request_id: str,
    req: RejectApprovalRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Reject a pending request.

    The reviewer identity is the authenticated user — never the request body.
    """
    from app.services.governance.approval_service import ApprovalService
    service = ApprovalService(db)
    reviewed_by = user.full_name or user.id
    try:
        request = service.reject(request_id, reviewed_by=reviewed_by, reason=req.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not request:
        raise HTTPException(status_code=404, detail="Request not found")
    return request.to_dict()


# --- Cost ---

@router.get("/governance/cost")
def get_cost_summary(
    execution_id: Optional[str] = Query(None),
    conversation_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Get total cost summary."""
    from app.services.governance.cost_service import CostService
    service = CostService(db)
    return service.get_total_cost(
        execution_id=execution_id,
        conversation_id=conversation_id,
    )


@router.get("/governance/cost/breakdown")
def get_cost_breakdown(
    execution_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Get cost breakdown by type."""
    from app.services.governance.cost_service import CostService
    service = CostService(db)
    return service.get_cost_breakdown(execution_id=execution_id)


# --- Audit ---

@router.get("/governance/audit")
def list_audit_logs(
    actor_type: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    resource_type: Optional[str] = Query(None),
    resource_id: Optional[str] = Query(None),
    execution_id: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """List audit logs with optional filters."""
    from app.services.governance.audit_service import AuditService
    service = AuditService(db)
    logs = service.list_logs(
        actor_type=actor_type,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        execution_id=execution_id,
        limit=limit,
        offset=offset,
    )
    return [l.to_dict() for l in logs]


@router.post("/governance/audit")
def record_audit(
    req: RecordAuditRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Record an audit log entry.

    The actor is always the authenticated user — ``actor_type``/``actor_id``/
    ``actor_name`` are no longer accepted from the request body, so audit
    entries cannot be forged to impersonate another user.
    """
    from app.services.governance.audit_service import AuditService
    service = AuditService(db)
    entry = service.record(
        actor_type="user",
        action=req.action,
        resource_type=req.resource_type,
        resource_id=req.resource_id,
        actor_id=user.id,
        actor_name=user.full_name,
        description=req.description,
        details_json=req.details_json,
        execution_id=req.execution_id,
        result=req.result,
    )
    return entry.to_dict()
