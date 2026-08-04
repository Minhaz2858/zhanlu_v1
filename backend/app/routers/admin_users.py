"""Admin user-management router (RBAC Phase 2, plan 2026-08-03).

All endpoints require the ``admin`` role (enforced by ``require_admin``).
Normal users get 403.  Supports listing, creating, updating (role/status)
and soft-deleting user accounts.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, require_admin
from app.models.user import User
from app.models.project import Project
from app.models.agent_app import AgentApp
from app.models.resource_share import ResourceShare
from app.services.auth_service import auth_service
from app.config import settings

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


# ── Request schemas ────────────────────────────────────────────────────
class CreateUserRequest(BaseModel):
    email: str
    full_name: str
    password: str
    role: str = "user"


class UpdateUserRequest(BaseModel):
    email: str | None = None
    full_name: str | None = None
    role: str | None = None
    password: str | None = None


def _serialize_user(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "full_name": u.full_name,
        "role": u.role,
        "created_date": u.created_date.isoformat() if u.created_date else None,
        "updated_date": u.updated_date.isoformat() if u.updated_date else None,
    }


# ── GET /admin/users — list all ────────────────────────────────────────
@router.get("")
async def list_users(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """List all non-deleted users (admin only)."""
    users = db.query(User).filter(User.is_deleted == False).order_by(User.created_date).all()
    return [_serialize_user(u) for u in users]


# ── GET /admin/users/{user_id} — detail with owned + granted resources ──
@router.get("/{user_id}")
async def get_user(
    user_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Get a single user with owned and granted resource lists (admin only)."""
    user = db.query(User).filter(
        User.id == user_id, User.is_deleted == False
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Owned resources
    owned_projects = db.query(Project).filter(
        Project.created_by_id == user_id, Project.is_deleted == False
    ).order_by(Project.name).all()
    owned_agents = db.query(AgentApp).filter(
        AgentApp.created_by_id == user_id, AgentApp.is_deleted == False
    ).order_by(AgentApp.name).all()

    owned = []
    for p in owned_projects:
        owned.append({"type": "project", "id": p.id, "name": p.name})
    for a in owned_agents:
        owned.append({"type": "agent", "id": a.id, "name": a.name})

    # Granted resources (via ResourceShare)
    shares = db.query(ResourceShare).filter(
        ResourceShare.shared_with_user_id == user_id,
        ResourceShare.is_deleted == False,
    ).order_by(ResourceShare.created_date.desc()).all()

    granted = []
    for s in shares:
        grantor = db.query(User).filter(User.id == s.created_by_id).first()
        grantor_email = grantor.email if grantor else "unknown"
        # Resolve resource name
        name = "Unknown"
        if s.resource_type == "project":
            r = db.query(Project).filter(Project.id == s.resource_id).first()
        else:
            r = db.query(AgentApp).filter(AgentApp.id == s.resource_id).first()
        if r:
            name = r.name
        granted.append({
            "type": s.resource_type,
            "id": s.resource_id,
            "name": name,
            "share_id": s.id,
            "granted_by_email": grantor_email,
        })

    result = _serialize_user(user)
    result["owned"] = owned
    result["granted"] = granted
    return result


# ── POST /admin/users — create ─────────────────────────────────────────
@router.post("")
async def create_user(
    body: CreateUserRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Create a new user (admin only)."""
    existing = db.query(User).filter(
        User.email == body.email, User.is_deleted == False
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="A user with that email already exists")

    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=422, detail="role must be 'admin' or 'user'")

    user = User(
        email=body.email,
        full_name=body.full_name,
        role=body.role,
        password_hash=auth_service.hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _serialize_user(user)


# ── PUT /admin/users/{id} — update ─────────────────────────────────────
@router.put("/{user_id}")
async def update_user(
    user_id: str,
    body: UpdateUserRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Update a user's email, name, role, or password (admin only)."""
    user = db.query(User).filter(
        User.id == user_id, User.is_deleted == False
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if body.email is not None:
        clash = db.query(User).filter(
            User.email == body.email, User.id != user_id, User.is_deleted == False
        ).first()
        if clash:
            raise HTTPException(status_code=409, detail="Email already in use")
        user.email = body.email

    if body.full_name is not None:
        user.full_name = body.full_name

    if body.role is not None:
        if body.role not in ("admin", "user"):
            raise HTTPException(status_code=422, detail="role must be 'admin' or 'user'")
        user.role = body.role

    if body.password is not None:
        user.password_hash = auth_service.hash_password(body.password)

    db.commit()
    db.refresh(user)
    return _serialize_user(user)


# ── DELETE /admin/users/{id} — soft delete ─────────────────────────────
@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Soft-delete a user (admin only). Prevents self-deletion."""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")

    user = db.query(User).filter(
        User.id == user_id, User.is_deleted == False
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_deleted = True
    db.commit()
    return {"deleted": True, "id": user_id}
