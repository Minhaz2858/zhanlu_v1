"""Approval service — manages approval workflow for high-risk actions.

When the PolicyEvaluator returns "require_confirm", an ApprovalRequest
is created.  Execution pauses until the user approves or rejects it.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.governance import ApprovalRequest, APPROVAL_STATUSES

logger = logging.getLogger(__name__)


class ApprovalService:
    """Service for managing approval requests."""

    def __init__(self, db: Session):
        self.db = db

    def create_request(
        self,
        action_type: str,
        action_description: str,
        risk_tier: str = "medium",
        execution_id: Optional[str] = None,
        plan_node_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
        context_json: Optional[dict] = None,
        ttl_hours: int = 24,
        org_id: str = "default-org",
        app_id: str = "default-app",
    ) -> ApprovalRequest:
        """Create a new approval request (pending status)."""
        request = ApprovalRequest(
            id=str(uuid4()),
            action_type=action_type,
            action_description=action_description,
            risk_tier=risk_tier,
            execution_id=execution_id,
            plan_node_id=plan_node_id,
            artifact_id=artifact_id,
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
            context_json=context_json,
            org_id=org_id,
            app_id=app_id,
        )
        self.db.add(request)
        self.db.commit()
        self.db.refresh(request)

        logger.info("Approval request %s created (action=%s, risk=%s)", request.id, action_type, risk_tier)
        return request

    def get_request(self, request_id: str) -> Optional[ApprovalRequest]:
        """Get an approval request by ID."""
        return self.db.query(ApprovalRequest).filter(ApprovalRequest.id == request_id).first()

    def list_requests(
        self,
        status: Optional[str] = None,
        execution_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[ApprovalRequest]:
        """List approval requests with optional filters."""
        query = self.db.query(ApprovalRequest).filter(ApprovalRequest.is_deleted == False)
        if status:
            query = query.filter(ApprovalRequest.status == status)
        if execution_id:
            query = query.filter(ApprovalRequest.execution_id == execution_id)
        return query.order_by(ApprovalRequest.created_date.desc()).limit(limit).all()

    def approve(
        self,
        request_id: str,
        reviewed_by: str,
        notes: Optional[str] = None,
    ) -> Optional[ApprovalRequest]:
        """Approve a pending request."""
        request = self.get_request(request_id)
        if not request:
            return None
        if request.status != "pending":
            raise ValueError(f"Request is already {request.status}")

        request.status = "approved"
        request.reviewed_by = reviewed_by
        request.reviewed_at = datetime.now(timezone.utc)
        request.review_notes = notes

        self.db.commit()
        self.db.refresh(request)
        logger.info("Approval request %s approved by %s", request_id, reviewed_by)
        return request

    def reject(
        self,
        request_id: str,
        reviewed_by: str,
        reason: str,
    ) -> Optional[ApprovalRequest]:
        """Reject a pending request."""
        request = self.get_request(request_id)
        if not request:
            return None
        if request.status != "pending":
            raise ValueError(f"Request is already {request.status}")

        request.status = "rejected"
        request.reviewed_by = reviewed_by
        request.reviewed_at = datetime.now(timezone.utc)
        request.review_notes = reason

        self.db.commit()
        self.db.refresh(request)
        logger.info("Approval request %s rejected by %s: %s", request_id, reviewed_by, reason)
        return request

    def expire_old_requests(self) -> int:
        """Mark expired requests as expired."""
        now = datetime.now(timezone.utc)
        expired = (
            self.db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.status == "pending",
                ApprovalRequest.expires_at < now,
            )
            .all()
        )
        for req in expired:
            req.status = "expired"

        if expired:
            self.db.commit()
            logger.info("Expired %d old approval requests", len(expired))
        return len(expired)
