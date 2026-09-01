"""Project-scoped memory review/edit API.

Exposes the agent's persistent memory (AgentMemory rows) as a per-project
CRUD surface so users can see, edit, pin, and delete what the agent
remembers — WITHOUT going through the LLM memory tool.

Scope rules (mirror memory_tool.py):
  * ``target='memory'`` rows are STRICTLY project-scoped: a request for
    project P only sees rows with project_id == P.  No NULL fallback —
    prevents the cross-project leak that surfaced with legacy rows.
  * ``target='user'`` rows are always cross-project (they describe WHO
    the user is), so they are listed only when ``include_user_profile``
    is set, and writes to them never carry a project_id.

All endpoints require the caller to be a logged-in user of the app.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_optional
from app.models.agent_memory import AgentMemory
from app.models.user import User

router = APIRouter(prefix="/projects/{project_id}/memories", tags=["memories"])


# ── Pydantic shapes ──────────────────────────────────────────────────────

class MemoryEntryOut(BaseModel):
    id: str
    agent_app_id: str
    user_id: Optional[str] = None
    project_id: Optional[str] = None
    target: str
    content: str
    char_count: int
    importance: int
    ttl_days: Optional[int] = None
    usage_count: int
    pinned: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MemoryListOut(BaseModel):
    project_id: str
    entries: list[MemoryEntryOut]
    total_chars: int
    limit_chars: int
    usage_pct: int
    include_user_profile: bool


class MemoryUpdateIn(BaseModel):
    content: Optional[str] = Field(default=None, max_length=2200)
    pinned: Optional[bool] = None
    importance: Optional[int] = Field(default=None, ge=0, le=10)


class MemoryCreateIn(BaseModel):
    content: str = Field(min_length=1, max_length=2200)
    target: str = Field(default="memory", pattern="^(memory|user)$")
    pinned: bool = False
    importance: int = Field(default=0, ge=0, le=10)


# ── Helpers ──────────────────────────────────────────────────────────────

def _to_out(mem: AgentMemory) -> MemoryEntryOut:
    return MemoryEntryOut(
        id=str(mem.id),
        agent_app_id=mem.agent_app_id,
        user_id=mem.user_id,
        project_id=mem.project_id,
        target=mem.target,
        content=mem.content or "",
        char_count=mem.char_count or 0,
        importance=mem.importance or 0,
        ttl_days=mem.ttl_days,
        usage_count=mem.usage_count or 0,
        pinned=bool(getattr(mem, "pinned", False)),
        created_at=mem.created_date.isoformat() if mem.created_date else None,
        updated_at=mem.updated_date.isoformat() if mem.updated_date else None,
    )


def _require_user(user: Optional[User]) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


# ── Endpoints ────────────────────────────────────────────────────────────

@router.get("", response_model=MemoryListOut)
def list_project_memories(
    project_id: str,
    include_user_profile: bool = Query(False, description="Also list the cross-project user profile"),
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """List all memory entries for this project.

    STRICT project scoping: only rows whose project_id == project_id are
    returned for target='memory'.  target='user' rows (the profile) are
    included only when ``include_user_profile=true``.
    """
    _require_user(user)
    query = db.query(AgentMemory).filter(
        AgentMemory.is_deleted == False,  # noqa: E712
    )
    if include_user_profile:
        query = query.filter(
            (AgentMemory.project_id == project_id)
            | (AgentMemory.target == "user")
        )
    else:
        query = query.filter(AgentMemory.project_id == project_id)
    rows = query.order_by(AgentMemory.updated_date.desc()).all()

    total_chars = sum(len(m.content or "") for m in rows if m.target == "memory")
    limit_chars = 2200  # MEMORY_CHAR_LIMIT from memory_tool
    usage_pct = min(100, int((total_chars / limit_chars) * 100)) if limit_chars else 0
    return MemoryListOut(
        project_id=project_id,
        entries=[_to_out(m) for m in rows],
        total_chars=total_chars,
        limit_chars=limit_chars,
        usage_pct=usage_pct,
        include_user_profile=include_user_profile,
    )


@router.get("/{memory_id}", response_model=MemoryEntryOut)
def get_project_memory(
    project_id: str,
    memory_id: str,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    _require_user(user)
    mem = db.query(AgentMemory).filter(
        AgentMemory.id == memory_id,
        AgentMemory.is_deleted == False,  # noqa: E712
    ).first()
    if mem is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    # Scope check: memory rows must match the project; user rows allowed.
    if mem.target != "user" and mem.project_id != project_id:
        raise HTTPException(status_code=404, detail="Memory not found in this project")
    return _to_out(mem)


@router.post("", response_model=MemoryEntryOut, status_code=201)
def create_project_memory(
    project_id: str,
    body: MemoryCreateIn,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Add a memory entry for this project (user-authored, no LLM needed)."""
    _require_user(user)
    from app.services.tool_security import scan_memory_content

    is_safe, detected = scan_memory_content(body.content)
    if not is_safe:
        raise HTTPException(status_code=400, detail=f"Content blocked: {detected}")

    # Security scan passed; dedupe exact duplicates within scope.
    existing = db.query(AgentMemory).filter(
        AgentMemory.is_deleted == False,  # noqa: E712
        AgentMemory.target == body.target,
        AgentMemory.content == body.content,
    )
    if body.target == "user":
        existing = existing.filter(AgentMemory.user_id == user.id)
    else:
        existing = existing.filter(AgentMemory.project_id == project_id)
    if existing.first() is not None:
        raise HTTPException(status_code=409, detail="Duplicate memory entry")

    mem = AgentMemory(
        agent_app_id="default",
        user_id=user.id,
        project_id=None if body.target == "user" else project_id,
        target=body.target,
        content=body.content,
        char_count=len(body.content),
        importance=body.importance,
    )
    db.add(mem)
    db.commit()
    db.refresh(mem)
    return _to_out(mem)


@router.patch("/{memory_id}", response_model=MemoryEntryOut)
def update_project_memory(
    project_id: str,
    memory_id: str,
    body: MemoryUpdateIn,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    _require_user(user)
    mem = db.query(AgentMemory).filter(
        AgentMemory.id == memory_id,
        AgentMemory.is_deleted == False,  # noqa: E712
    ).first()
    if mem is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    if mem.target != "user" and mem.project_id != project_id:
        raise HTTPException(status_code=404, detail="Memory not found in this project")

    if body.content is not None:
        from app.services.tool_security import scan_memory_content

        is_safe, detected = scan_memory_content(body.content)
        if not is_safe:
            raise HTTPException(status_code=400, detail=f"Content blocked: {detected}")
        mem.content = body.content
        mem.char_count = len(body.content)
    if body.pinned is not None:
        mem.pinned = body.pinned
    if body.importance is not None:
        mem.importance = body.importance
    db.commit()
    db.refresh(mem)
    return _to_out(mem)


@router.delete("/{memory_id}")
def delete_project_memory(
    project_id: str,
    memory_id: str,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Hard-delete a memory entry (user's memory is theirs to remove)."""
    _require_user(user)
    mem = db.query(AgentMemory).filter(
        AgentMemory.id == memory_id,
        AgentMemory.is_deleted == False,  # noqa: E712
    ).first()
    if mem is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    if mem.target != "user" and mem.project_id != project_id:
        raise HTTPException(status_code=404, detail="Memory not found in this project")
    # Hard delete (matches user preference: no soft-delete orphans).
    db.delete(mem)
    db.commit()
    return {"success": True, "deleted": memory_id}
