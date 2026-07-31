"""Hooks API — session-auth, org-scoped CRUD for HookRule + live reload.

After any mutation (create/update/delete) the live ``HookExecutor`` is
reloaded via ``load_hooks(db)`` so changes take effect immediately without
a backend restart.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user_required
from app.models.hook_rule import HookRule
from app.services.hooks.loader import load_hooks

router = APIRouter(prefix="/hooks", tags=["hooks"])

_VALID_EVENTS = {
    "session_start", "session_end", "pre_compact", "post_compact",
    "pre_tool_use", "post_tool_use", "user_prompt_submit",
    "notification", "stop", "subagent_stop",
}
_VALID_TYPES = {"command", "http", "prompt", "agent"}


class HookRuleCreate(BaseModel):
    name: str
    description: str | None = None
    event: str
    type: str
    command: str | None = None
    url: str | None = None
    method: str = "POST"
    headers: dict[str, str] | None = None
    prompt: str | None = None
    timeout: int = 30
    priority: int = 0
    matcher: str | None = None
    block_on_failure: bool = False
    enabled: bool = True


class HookRuleUpdate(HookRuleCreate):
    """Full-replace update shape (PUT)."""


def _validate(payload: HookRuleCreate) -> None:
    if payload.event not in _VALID_EVENTS:
        raise HTTPException(status_code=400, detail=f"event must be one of {sorted(_VALID_EVENTS)}")
    if payload.type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"type must be one of {sorted(_VALID_TYPES)}")
    if payload.type == "command" and not payload.command:
        raise HTTPException(status_code=400, detail="command-type hook requires 'command'")
    if payload.type == "http" and not payload.url:
        raise HTTPException(status_code=400, detail="http-type hook requires 'url'")
    if payload.type in ("prompt", "agent") and not payload.prompt:
        raise HTTPException(status_code=400, detail="prompt/agent-type hook requires 'prompt'")


def _scoped(db: Session, user, hook_id: str) -> HookRule:
    h = db.query(HookRule).filter(
        HookRule.id == hook_id,
        HookRule.org_id == user.org_id,
        HookRule.is_deleted == False,  # noqa: E712
    ).first()
    if h is None:
        raise HTTPException(status_code=404, detail="Hook rule not found")
    return h


@router.post("", status_code=201)
def create_hook(payload: HookRuleCreate, db: Session = Depends(get_db),
                user=Depends(get_current_user_required)):
    _validate(payload)
    h = HookRule(
        name=payload.name,
        description=payload.description,
        event=payload.event,
        type=payload.type,
        command=payload.command,
        url=payload.url,
        method=payload.method,
        headers=payload.headers,
        prompt=payload.prompt,
        timeout=payload.timeout,
        priority=payload.priority,
        matcher=payload.matcher,
        block_on_failure=payload.block_on_failure,
        enabled=payload.enabled,
        org_id=user.org_id,
        app_id=getattr(user, "app_id", "default-app"),
        created_by_id=user.id,
    )
    db.add(h)
    db.commit()
    db.refresh(h)
    load_hooks(db)
    return h.to_dict()


@router.get("")
def list_hooks(db: Session = Depends(get_db),
               user=Depends(get_current_user_required)):
    q = db.query(HookRule).filter(
        HookRule.org_id == user.org_id,
        HookRule.is_deleted == False,  # noqa: E712
    )
    return [h.to_dict() for h in q.order_by(HookRule.event, HookRule.priority.desc()).limit(1000)]


@router.get("/{hook_id}")
def get_hook(hook_id: str, db: Session = Depends(get_db),
             user=Depends(get_current_user_required)):
    return _scoped(db, user, hook_id).to_dict()


@router.put("/{hook_id}")
def update_hook(hook_id: str, payload: HookRuleUpdate, db: Session = Depends(get_db),
                user=Depends(get_current_user_required)):
    h = _scoped(db, user, hook_id)
    _validate(payload)
    h.name = payload.name
    h.description = payload.description
    h.event = payload.event
    h.type = payload.type
    h.command = payload.command
    h.url = payload.url
    h.method = payload.method
    h.headers = payload.headers
    h.prompt = payload.prompt
    h.timeout = payload.timeout
    h.priority = payload.priority
    h.matcher = payload.matcher
    h.block_on_failure = payload.block_on_failure
    h.enabled = payload.enabled
    db.commit()
    db.refresh(h)
    load_hooks(db)
    return h.to_dict()


@router.delete("/{hook_id}", status_code=204)
def delete_hook(hook_id: str, db: Session = Depends(get_db),
                user=Depends(get_current_user_required)):
    h = _scoped(db, user, hook_id)
    h.is_deleted = True
    db.commit()
    load_hooks(db)
    return None
