"""Audit service — append-only audit log for compliance.

Every significant action (create, update, delete, execute, approve, reject,
publish) is recorded in the audit_logs table.  Records are never updated
or deleted — they provide a complete trail for compliance and debugging.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.governance import AuditLog

logger = logging.getLogger(__name__)


class AuditService:
    """Service for recording audit log entries."""

    def __init__(self, db: Session):
        self.db = db

    def record(
        self,
        actor_type: str,
        action: str,
        resource_type: str,
        resource_id: Optional[str] = None,
        actor_id: Optional[str] = None,
        actor_name: Optional[str] = None,
        description: Optional[str] = None,
        details_json: Optional[dict] = None,
        execution_id: Optional[str] = None,
        result: str = "success",
        error_message: Optional[str] = None,
        org_id: str = "default-org",
        app_id: str = "default-app",
    ) -> AuditLog:
        """Record an audit log entry (append-only)."""
        entry = AuditLog(
            id=str(uuid4()),
            actor_type=actor_type,  # user | agent | system
            actor_id=actor_id,
            actor_name=actor_name,
            action=action,  # create | update | delete | execute | approve | reject | publish
            resource_type=resource_type,  # agent | skill | artifact | execution | policy
            resource_id=resource_id,
            description=description,
            details_json=details_json,
            execution_id=execution_id,
            result=result,  # success | failure | denied
            error_message=error_message,
            occurred_at=datetime.now(timezone.utc),
            org_id=org_id,
            app_id=app_id,
        )
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)

        logger.info(
            "Audit: %s/%s by %s/%s on %s/%s — %s",
            action, resource_type, actor_type, actor_name or actor_id,
            resource_type, resource_id or "—", result,
        )
        return entry

    def list_logs(
        self,
        actor_type: Optional[str] = None,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]:
        """List audit logs with optional filters."""
        query = self.db.query(AuditLog)

        if actor_type:
            query = query.filter(AuditLog.actor_type == actor_type)
        if action:
            query = query.filter(AuditLog.action == action)
        if resource_type:
            query = query.filter(AuditLog.resource_type == resource_type)
        if resource_id:
            query = query.filter(AuditLog.resource_id == resource_id)
        if execution_id:
            query = query.filter(AuditLog.execution_id == execution_id)

        query = query.order_by(AuditLog.occurred_at.desc())
        return query.offset(offset).limit(limit).all()
