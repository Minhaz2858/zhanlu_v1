"""Resource sharing router (RBAC Phase 2, plan 2026-08-03).

Lets owners share their Projects / AgentApps with other users for
view + use access.  Edit / delete / re-share remain owner-only
(enforced in entities.py write guards).

Endpoints:
  POST   /api/shares          — share a resource
  GET    /api/shares          — list shares I gave or received
  DELETE /api/shares/{id}     — revoke a share I gave
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required
from app.models.user import User
from app.models.resource_share import ResourceShare
from app.models.resource_access_policy import ResourceAccessPolicy

router = APIRouter(prefix="/shares", tags=["resource-shares"])

# Map frontend resource_type values to the polymorphic discriminator
# stored on ResourceShare.resource_type.
_RESOURCE_TYPES = {"project": "project", "agent": "agent"}


class ShareRequest(BaseModel):
    resource_type: str   # "project" | "agent"
    resource_id: str
    shared_with_user_id: str
    access_level: str = "use"


def _serialize_share(s: ResourceShare, db: Session) -> dict:
    out = {
        "id": s.id,
        "resource_type": s.resource_type,
        "resource_id": s.resource_id,
        "shared_with_user_id": s.shared_with_user_id,
        "shared_by_user_id": s.created_by_id,
        "access_level": s.access_level,
        "created_date": s.created_date.isoformat() if s.created_date else None,
    }
    # Hydrate names for display
    recip = db.query(User).filter(User.id == s.shared_with_user_id).first()
    if recip:
        out["shared_with_email"] = recip.email
        out["shared_with_name"] = recip.full_name
    sharer = db.query(User).filter(User.id == s.created_by_id).first()
    if sharer:
        out["shared_by_email"] = sharer.email
        out["shared_by_name"] = sharer.full_name
    return out


# ── POST /shares ───────────────────────────────────────────────────────
@router.post("")
async def create_share(
    body: ShareRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Share a resource with another user.

    Rules:
    - Caller must own the resource (created_by_id == caller) OR be admin.
    - Cannot share with yourself.
    - Cannot create a duplicate active share.
    """
    rt = _RESOURCE_TYPES.get(body.resource_type)
    if rt is None:
        raise HTTPException(
            status_code=422,
            detail=f"resource_type must be one of: {list(_RESOURCE_TYPES)}",
        )

    if body.shared_with_user_id == user.id:
        raise HTTPException(status_code=400, detail="You cannot share a resource with yourself")

    # Verify the recipient exists
    recipient = db.query(User).filter(
        User.id == body.shared_with_user_id, User.is_deleted == False
    ).first()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient user not found")

    # Verify the resource exists
    from app.models.project import Project
    from app.models.agent_app import AgentApp
    model_map = {"project": Project, "agent": AgentApp}
    model = model_map[rt]
    resource = db.query(model).filter(
        model.id == body.resource_id, model.is_deleted == False
    ).first()
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")

    # Admin can share any resource; normal users must own it
    if user.role != "admin" and getattr(resource, "created_by_id", None) != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the owner can share this resource",
        )

    # Prevent duplicates
    existing = db.query(ResourceShare).filter(
        ResourceShare.resource_type == rt,
        ResourceShare.resource_id == body.resource_id,
        ResourceShare.shared_with_user_id == body.shared_with_user_id,
        ResourceShare.is_deleted == False,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="This resource is already shared with that user")

    share = ResourceShare(
        resource_type=rt,
        resource_id=body.resource_id,
        shared_with_user_id=body.shared_with_user_id,
        access_level=body.access_level,
        created_by_id=user.id,
    )
    db.add(share)

    # When a resource is shared, auto-promote it to "company" so it appears
    # under Company Projects / Company Agents for all users (not just the
    # recipient).  Once flipped it stays "company" even if all shares are
    # later revoked (stable design per plan 2026-08-05).
    if getattr(resource, "resource_type", None) == "personal":
        resource.resource_type = "company"

    db.commit()
    db.refresh(share)
    return _serialize_share(share, db)


# ── GET /shares ────────────────────────────────────────────────────────
@router.get("")
async def list_shares(
    resource_type: str | None = None,
    resource_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """List all shares: ones I gave (as owner) + ones I received.
    
    Optional query params ``resource_type`` + ``resource_id`` filter to a
    specific resource (used by the admin Manage Access dialog).
    """
    q = db.query(ResourceShare).filter(ResourceShare.is_deleted == False)

    if resource_type and resource_id:
        q = q.filter(
            ResourceShare.resource_type == resource_type,
            ResourceShare.resource_id == resource_id,
        )

    given = q.filter(ResourceShare.created_by_id == user.id).order_by(
        ResourceShare.created_date.desc()
    ).all()

    # Re-query for received without the resource filter (user always wants
    # all shares they received, not limited to a single resource).
    received_q = db.query(ResourceShare).filter(
        ResourceShare.shared_with_user_id == user.id,
        ResourceShare.is_deleted == False,
    )
    if resource_type and resource_id:
        received_q = received_q.filter(
            ResourceShare.resource_type == resource_type,
            ResourceShare.resource_id == resource_id,
        )
    received = received_q.order_by(ResourceShare.created_date.desc()).all()

    return {
        "given": [_serialize_share(s, db) for s in given],
        "received": [_serialize_share(s, db) for s in received],
    }


# ── DELETE /shares/{id} ────────────────────────────────────────────────
@router.delete("/{share_id}")
async def revoke_share(
    share_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Revoke a share.  The user who created the share (the owner) or an
    admin can revoke any share.  A recipient can NOT revoke a share they
    received — they can only ask the owner/admin to revoke it."""
    share = db.query(ResourceShare).filter(
        ResourceShare.id == share_id, ResourceShare.is_deleted == False
    ).first()
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")

    # Admin can revoke any share; normal users can only revoke their own
    if user.role != "admin" and share.created_by_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the owner or an admin can revoke this share",
        )

    share.is_deleted = True

    # Cascade: soft-delete every access policy attached to this share.  Without
    # this, stale policies would linger and re-constrain the user if the share
    # is later re-created (partial unique index only covers active rows).
    db.query(ResourceAccessPolicy).filter(
        ResourceAccessPolicy.resource_share_id == share_id,
        ResourceAccessPolicy.is_deleted == False,  # noqa: E712
    ).update({"is_deleted": True}, synchronize_session=False)

    db.commit()
    return {"revoked": True, "id": share_id}
