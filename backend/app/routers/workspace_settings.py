"""Workspace settings router — read/write workspace-level flags.

Backs the ``auto_bind_all_datasources`` opt-in flag (DEPRECATED — Phase 7C:
the flag silently unions every workspace KB into every agent's scope,
contradicting the Agent-Harness invariant that an agent selects among
EXPLICITLY authorized sources. It stays default-OFF, is now admin-gated,
and is slated for removal once workspace owners migrate to explicit agent
bindings). Lives at ``/api/workspace-settings`` so the frontend can hit a
small, typed endpoint rather than the generic entity router. The actual
storage is the ``workspace_settings`` table (see ``app.models.workspace_settings``)
read/written through ``workspace_settings_service``.

Phase 7C authorization (B2):
- the org is resolved from the VERIFIED user — a client-supplied ``org_id``
  that mismatches is refused (403), never honored;
- writes (PUT) additionally require an org admin — the flag changes the
  data scope of every agent in the org at once;
- reads stay available to every org member (the frontend settings page).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required, require_org_admin
from app.services import workspace_settings_service

router = APIRouter(prefix="/workspace-settings", tags=["workspace-settings"])


class WorkspaceSettingsPayload(BaseModel):
    auto_bind_all_datasources: Optional[bool] = None


def _resolve_org(user, org_id: str) -> str:
    """Phase 7C: the verified user's org is authoritative. A mismatching
    client-supplied org_id is a cross-tenant attempt → 403."""
    user_org = getattr(user, "org_id", None) or "default-org"
    if org_id and org_id != user_org:
        raise HTTPException(
            status_code=403,
            detail="workspace settings are org-scoped — cross-org access refused")
    return user_org


@router.get("")
def get_settings(
    org_id: str = "",
    app_id: str = "default-app",
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
) -> dict:
    """Return the current workspace settings as a typed JSON object."""
    org = _resolve_org(user, org_id)
    return {
        "auto_bind_all_datasources": workspace_settings_service.get_bool(
            db,
            workspace_settings_service.KEY_AUTO_BIND_ALL_DATASOURCES,
            org_id=org,
            app_id=app_id,
        ),
    }


@router.put("")
def update_settings(
    payload: WorkspaceSettingsPayload,
    org_id: str = "",
    app_id: str = "default-app",
    db: Session = Depends(get_db),
    admin=Depends(require_org_admin),
) -> dict:
    """Upsert the workspace settings. Only fields explicitly set in
    the body are updated. Org-admin only (Phase 7C).
    """
    org = _resolve_org(admin, org_id)
    if payload.auto_bind_all_datasources is not None:
        workspace_settings_service.set_value(
            db,
            workspace_settings_service.KEY_AUTO_BIND_ALL_DATASOURCES,
            "true" if payload.auto_bind_all_datasources else "false",
            org_id=org,
            app_id=app_id,
        )
        db.commit()
    return get_settings(org_id=org, app_id=app_id, db=db, user=admin)
