"""Admin CRUD for llm_models catalog + effective-llm query endpoint.

Gated by ``HIERARCHICAL_LLM_ENABLED`` — returns 403 when the flag is off.
All mutation endpoints require admin role.
"""

from __future__ import annotations

import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_db, get_current_user_required
from app.models.llm_model import LlmModel
from app.models.user import User
from app.services.crypto_utils import encrypt_value, decrypt_value
from app.services.llm_router import resolve_effective_llm, EffectiveLLM, LLMEndpoint
from app.services.llm_service import test_llm_endpoint

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/llm", tags=["llm"])


# ── Pydantic schemas ────────────────────────────────────────────────────

class LlmModelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    model_id: str = Field(..., min_length=1, max_length=120)
    provider: str = Field(..., min_length=1, max_length=60)
    base_url: str = Field(..., min_length=1, max_length=255)
    api_key: str | None = None
    is_private: bool = False
    is_default: bool = False
    enabled: bool = True


class LlmModelUpdate(BaseModel):
    name: str | None = None
    model_id: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    is_private: bool | None = None
    is_default: bool | None = None
    enabled: bool | None = None


class TestConnectionBody(BaseModel):
    """Unsaved dialog values for the "test connection" probe."""
    name: str | None = None
    base_url: str = Field(..., min_length=1, max_length=255)
    api_key: str | None = None
    model_id: str = Field(..., min_length=1, max_length=120)
    provider: str | None = None


class LlmModelOut(BaseModel):
    id: str
    name: str
    model_id: str
    provider: str
    base_url: str
    api_key: str | None = None  # always masked — "••••••" when set
    is_private: bool
    is_default: bool
    enabled: bool
    created_date: str | None = None
    updated_date: str | None = None
    created_by_id: str | None = None

    class Config:
        from_attributes = True


class EffectiveLLMOut(BaseModel):
    model_name: str
    model_id: str
    provider: str = ""
    base_url: str = ""
    is_private: bool = False
    locked: bool = False
    locked_reason: str = ""
    legacy_fallback: bool = False  # True when flag is off or no catalog match


def _admin_only(user: User):
    """Raise 403 unless *user* has the admin role."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")


def _check_enabled():
    if not settings.HIERARCHICAL_LLM_ENABLED:
        raise HTTPException(status_code=403, detail="Hierarchical LLM config is disabled")


@router.get("/feature-status")
def feature_status(user: User = Depends(get_current_user_required)):
    """Returns whether the hierarchical LLM feature is enabled.

    Frontend uses this to show a clear "Feature disabled" message in the
    Configuration Center instead of an empty list with no explanation.
    Always accessible (does not require the feature to be on).
    """
    return {
        "enabled": bool(settings.HIERARCHICAL_LLM_ENABLED),
        "is_admin": user.role == "admin",
    }


def _to_out(row: LlmModel) -> LlmModelOut:
    return LlmModelOut(
        id=row.id,
        name=row.name,
        model_id=row.model_id,
        provider=row.provider,
        base_url=row.base_url,
        api_key="••••••" if row.api_key else None,
        is_private=row.is_private,
        is_default=row.is_default,
        enabled=row.enabled,
        created_date=row.created_date.isoformat() if row.created_date else None,
        updated_date=row.updated_date.isoformat() if row.updated_date else None,
        created_by_id=row.created_by_id,
    )


# ── Endpoints ────────────────────────────────────────────────────────────

@router.get("/models", response_model=list[LlmModelOut])
def list_models(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """List all llm_models for the current tenant. Admin + user access."""
    _check_enabled()
    rows = (
        db.query(LlmModel)
        .filter(
            LlmModel.is_deleted.is_(False),
            LlmModel.org_id == (user.org_id or "default-org"),
            LlmModel.app_id == (user.app_id or "default-app"),
        )
        .order_by(LlmModel.name.asc())
        .all()
    )
    return [_to_out(r) for r in rows]


@router.post("/models", response_model=LlmModelOut, status_code=201)
def create_model(
    body: LlmModelCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Create a new LLM model entry (admin only)."""
    _check_enabled()
    _admin_only(user)

    encrypted = encrypt_value(body.api_key) if body.api_key else None

    # Enforce at most one is_default per tenant
    if body.is_default:
        db.query(LlmModel).filter(
            LlmModel.is_default.is_(True),
            LlmModel.is_deleted.is_(False),
            LlmModel.org_id == (user.org_id or "default-org"),
        ).update({"is_default": False})

    row = LlmModel(
        id=str(uuid.uuid4()),
        org_id=user.org_id or "default-org",
        app_id=user.app_id or "default-app",
        created_by_id=user.id,
        name=body.name,
        model_id=body.model_id,
        provider=body.provider,
        base_url=body.base_url,
        api_key=encrypted,
        is_private=body.is_private,
        is_default=body.is_default,
        enabled=body.enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.put("/models/{model_id}", response_model=LlmModelOut)
def update_model(
    model_id: str,
    body: LlmModelUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Update an LLM model entry (admin only)."""
    _check_enabled()
    _admin_only(user)

    row = db.query(LlmModel).filter(
        LlmModel.id == model_id,
        LlmModel.is_deleted.is_(False),
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="LLM model not found")

    if body.name is not None:
        row.name = body.name
    if body.model_id is not None:
        row.model_id = body.model_id
    if body.provider is not None:
        row.provider = body.provider
    if body.base_url is not None:
        row.base_url = body.base_url
    if body.api_key is not None:
        row.api_key = encrypt_value(body.api_key)
    if body.is_private is not None:
        row.is_private = body.is_private
    if body.enabled is not None:
        row.enabled = body.enabled

    # is_default singleton
    if body.is_default is True:
        db.query(LlmModel).filter(
            LlmModel.is_default.is_(True),
            LlmModel.is_deleted.is_(False),
            LlmModel.org_id == row.org_id,
            LlmModel.id != row.id,
        ).update({"is_default": False})
        row.is_default = True

    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.delete("/models/{model_id}", status_code=204)
def delete_model(
    model_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Soft-delete an LLM model entry (admin only)."""
    _check_enabled()
    _admin_only(user)

    row = db.query(LlmModel).filter(
        LlmModel.id == model_id,
        LlmModel.is_deleted.is_(False),
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="LLM model not found")
    row.is_deleted = True
    db.commit()


@router.post("/models/{model_id}/test")
def test_model(
    model_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Probe a saved LLM model entry (admin only). Uses the decrypted api_key."""
    _check_enabled()
    _admin_only(user)

    row = db.query(LlmModel).filter(
        LlmModel.id == model_id,
        LlmModel.is_deleted.is_(False),
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="LLM model not found")

    api_key = decrypt_value(row.api_key)
    if row.api_key and not api_key:
        # Decryption failed — don't leak the exception (may contain key material).
        return {
            "ok": False,
            "latency_ms": None,
            "status_code": None,
            "response_text": None,
            "error": "Stored API key is invalid (could not be decrypted)",
        }
    return test_llm_endpoint(
        base_url=row.base_url,
        api_key=api_key,
        model=row.model_id,
    )


@router.post("/models/test-connection")
def test_connection(
    body: TestConnectionBody,
    user: User = Depends(get_current_user_required),
):
    """Probe an unsaved endpoint from the Add/Edit dialog (admin only).

    Values come straight from the request body; the api_key is never
    stored, logged, or echoed back.
    """
    _check_enabled()
    _admin_only(user)

    return test_llm_endpoint(
        base_url=body.base_url,
        api_key=body.api_key,
        model=body.model_id,
    )


@router.get("/effective")
def get_effective_llm(
    project_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    agent_name: str | None = Query(None),
    user_model: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Return the effective LLM that will be used for a chat request.

    Called by the frontend to update the chat header badge before
    the first message of a conversation.
    """
    _check_enabled()
    eff = resolve_effective_llm(
        db,
        project_id=project_id,
        agent_id=agent_id,
        agent_name=agent_name,
        user_model=user_model,
        user_is_admin=(user.role == "admin"),
        org_id=user.org_id or "default-org",
        app_id=user.app_id or "default-app",
    )
    return EffectiveLLMOut(
        model_name=eff.model_name,
        model_id=eff.model_id,
        provider="",
        base_url=eff.endpoint.base_url if eff.endpoint else (settings.LLM_BASE_URL or ""),
        is_private=eff.endpoint.is_private if eff.endpoint else False,
        locked=eff.locked,
        locked_reason=eff.locked_reason,
        legacy_fallback=eff.endpoint is None,
    )


@router.post("/apply-to-agent")
def apply_llm_to_agent(
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Set llm_model_id on a single agent.

    Used by the agent chip in ProjectDetail when the user clicks "apply".
    Admin only.
    """
    _check_enabled()
    _admin_only(user)

    agent_id = body.get("agent_id")
    model_id = body.get("model_id")  # None = clear
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id required")

    from app.models.agent_app import AgentApp
    row = db.query(AgentApp).filter(
        AgentApp.id == agent_id,
        AgentApp.is_deleted.is_(False),
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    row.llm_model_id = model_id
    db.commit()
    return {"updated": 1, "agent_id": agent_id, "llm_model_id": model_id}
