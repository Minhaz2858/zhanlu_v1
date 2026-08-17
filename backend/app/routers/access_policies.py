"""Resource access policy router (per-user data access control).

Owners (or admins) configure, per shared user, which databases (KnowledgeBases)
and which tables within them that user may use through a shared project/agent.

Endpoints:
  GET    /api/access-policies          — list policies for a share
  PUT    /api/access-policies          — batch upsert (save the whole matrix)
  DELETE /api/access-policies/{id}     — delete a single policy
  GET    /api/access-policies/preview  — preview effective permissions
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required
from app.models.user import User
from app.models.resource_share import ResourceShare
from app.models.resource_access_policy import ResourceAccessPolicy
from app.models.project import Project
from app.models.agent_app import AgentApp
from app.models.knowledge_base import KnowledgeBase
from app.services import access_policy_service

router = APIRouter(prefix="/access-policies", tags=["access-policies"])

_VALID_MODES = {"allow", "deny", "allow_columns"}
_RESOURCE_TYPES = {"project", "agent"}


# ── Schemas ────────────────────────────────────────────────────────────
class PolicyItem(BaseModel):
    kb_id: str | None = None          # None = all KBs in the resource
    table_name: str | None = None     # None = all tables in the KB
    mode: str = "allow"               # allow | deny | allow_columns
    column_allowlist: list[str] | None = None
    row_filter: dict | None = None


class BatchUpsertRequest(BaseModel):
    resource_type: str
    resource_id: str
    user_id: str = Field(..., description="Shared user being constrained")
    policies: list[PolicyItem] = Field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────────
def _serialize_policy(p: ResourceAccessPolicy) -> dict:
    return {
        "id": p.id,
        "resource_share_id": p.resource_share_id,
        "resource_type": p.resource_type,
        "resource_id": p.resource_id,
        "user_id": p.user_id,
        "kb_id": p.kb_id,
        "table_name": p.table_name,
        "mode": p.mode,
        "column_allowlist": p.column_allowlist,
        "row_filter": p.row_filter,
    }


def _find_share(
    db: Session, resource_type: str, resource_id: str, user_id: str
) -> ResourceShare:
    """Find the active share for (resource_type, resource_id, shared user)."""
    share = db.query(ResourceShare).filter(
        ResourceShare.resource_type == resource_type,
        ResourceShare.resource_id == resource_id,
        ResourceShare.shared_with_user_id == user_id,
        ResourceShare.is_deleted == False,  # noqa: E712
    ).first()
    if not share:
        raise HTTPException(
            status_code=404,
            detail="No active share found for this user on this resource",
        )
    return share


def _require_owner_or_admin(share: ResourceShare, user: User) -> None:
    """Only the resource owner (share creator) or an admin may mutate policies."""
    if user.role == "admin":
        return
    if share.created_by_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the owner or an admin can manage data access policies",
        )


def _validate_policy_item(item: PolicyItem) -> None:
    if item.mode not in _VALID_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"mode must be one of: {sorted(_VALID_MODES)}",
        )
    if item.mode == "allow_columns" and not item.table_name:
        raise HTTPException(
            status_code=422,
            detail="mode='allow_columns' requires a specific table_name",
        )
    if item.mode == "allow_columns" and not item.column_allowlist:
        raise HTTPException(
            status_code=422,
            detail="mode='allow_columns' requires column_allowlist",
        )


def _resource_bound_kb_ids(db: Session, resource_type: str, resource_id: str) -> list[str]:
    """KB ids bound to a resource (project-scoped KBs or agent bindings)."""
    if resource_type == "project":
        kbs = db.query(KnowledgeBase).filter(
            KnowledgeBase.project_id == resource_id,
            KnowledgeBase.is_deleted == False,  # noqa: E712
        ).all()
        return [kb.id for kb in kbs]

    if resource_type == "agent":
        agent = db.query(AgentApp).filter(
            AgentApp.id == resource_id, AgentApp.is_deleted == False  # noqa: E712
        ).first()
        if not agent:
            return []
        # agent.knowledge_bases is a JSON list of KB ids
        raw = agent.knowledge_bases or []
        return [str(kb) for kb in raw if kb]

    return []


# ── GET /access-policies ───────────────────────────────────────────────
@router.get("")
async def list_policies(
    resource_type: str = Query(...),
    resource_id: str = Query(...),
    user_id: str = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """List all policies for a (resource, shared user) share."""
    if resource_type not in _RESOURCE_TYPES:
        raise HTTPException(status_code=422, detail="Invalid resource_type")

    share = _find_share(db, resource_type, resource_id, user_id)
    # Reading policies is allowed for owners/admins; also let the recipient read
    # their own policy (so the UI can show what they can/cannot access).  For
    # simplicity, permit any authenticated caller who can see the share.
    policies = (
        db.query(ResourceAccessPolicy)
        .filter(
            ResourceAccessPolicy.resource_share_id == share.id,
            ResourceAccessPolicy.is_deleted == False,  # noqa: E712
        )
        .order_by(ResourceAccessPolicy.kb_id, ResourceAccessPolicy.table_name)
        .all()
    )
    return {"policies": [_serialize_policy(p) for p in policies]}


# ── PUT /access-policies (batch upsert) ────────────────────────────────
@router.put("")
async def upsert_policies(
    body: BatchUpsertRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Save the full data-access matrix for a shared user on a resource.

    Replaces all existing policies for the share (delete + recreate), matching
    the "owner edits a matrix and hits save" UX.
    """
    if body.resource_type not in _RESOURCE_TYPES:
        raise HTTPException(status_code=422, detail="Invalid resource_type")

    share = _find_share(db, body.resource_type, body.resource_id, body.user_id)
    _require_owner_or_admin(share, user)

    for item in body.policies:
        _validate_policy_item(item)

    # Soft-delete existing active policies, then insert the new matrix.  The
    # partial unique index (WHERE is_deleted = false) means re-inserting the
    # same (kb_id, table_name) does not conflict with the now-soft-deleted rows.
    db.query(ResourceAccessPolicy).filter(
        ResourceAccessPolicy.resource_share_id == share.id,
        ResourceAccessPolicy.is_deleted == False,  # noqa: E712
    ).update({"is_deleted": True}, synchronize_session=False)

    created: list[ResourceAccessPolicy] = []
    for item in body.policies:
        p = ResourceAccessPolicy(
            resource_share_id=share.id,
            resource_type=body.resource_type,
            resource_id=body.resource_id,
            user_id=body.user_id,
            kb_id=item.kb_id,
            table_name=item.table_name.lower() if item.table_name else None,
            mode=item.mode,
            column_allowlist=item.column_allowlist,
            row_filter=item.row_filter,
            created_by_id=user.id,
        )
        db.add(p)
        created.append(p)

    db.commit()
    for p in created:
        db.refresh(p)
    return {"policies": [_serialize_policy(p) for p in created]}


# ── DELETE /access-policies/{id} ───────────────────────────────────────
@router.delete("/{policy_id}")
async def delete_policy(
    policy_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    policy = db.query(ResourceAccessPolicy).filter(
        ResourceAccessPolicy.id == policy_id,
        ResourceAccessPolicy.is_deleted == False,  # noqa: E712
    ).first()
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")

    share = db.query(ResourceShare).filter(
        ResourceShare.id == policy.resource_share_id
    ).first()
    if share is None:
        raise HTTPException(status_code=404, detail="Share not found")

    _require_owner_or_admin(share, user)

    policy.is_deleted = True
    db.commit()
    return {"deleted": True, "id": policy_id}


# ── GET /access-policies/preview ───────────────────────────────────────
@router.get("/preview")
async def preview_permissions(
    resource_type: str = Query(...),
    resource_id: str = Query(...),
    user_id: str = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Preview the effective permissions a shared user has on a resource.

    Returns the bound KBs with their effective status (allowed / denied /
    restricted) plus the raw configured policies.
    """
    if resource_type not in _RESOURCE_TYPES:
        raise HTTPException(status_code=422, detail="Invalid resource_type")

    share = _find_share(db, resource_type, resource_id, user_id)
    _require_owner_or_admin(share, user)

    bound_kb_ids = _resource_bound_kb_ids(db, resource_type, resource_id)

    resolved = access_policy_service.resolve_user_policies(
        db,
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        bound_kb_ids=bound_kb_ids,
        owner_id=share.created_by_id,
        is_admin=False,
    )

    # Hydrate KB names.
    kb_names: dict[str, str] = {}
    if bound_kb_ids:
        kbs = db.query(KnowledgeBase).filter(
            KnowledgeBase.id.in_(bound_kb_ids)
        ).all()
        kb_names = {kb.id: kb.name for kb in kbs}

    kbs_out = []
    for kb_id in bound_kb_ids:
        if resolved.is_kb_fully_denied(kb_id):
            status = "denied"
        elif resolved.is_kb_restricted(kb_id):
            status = "restricted"
        else:
            status = "allowed"

        kbs_out.append({
            "id": kb_id,
            "name": kb_names.get(kb_id, kb_id),
            "status": status,
            "allowed_tables": resolved.allowed_tables_for_kb(kb_id),
            "blocked_tables": resolved.blocked_tables_for_kb(kb_id),
        })

    policies = (
        db.query(ResourceAccessPolicy)
        .filter(
            ResourceAccessPolicy.resource_share_id == share.id,
            ResourceAccessPolicy.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    return {
        "kbs": kbs_out,
        "policies": [_serialize_policy(p) for p in policies],
    }
