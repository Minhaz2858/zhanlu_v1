"""Workspace settings router — read/write workspace-level flags.

Backs the new ``auto_bind_all_datasources`` opt-in flag. Lives at
``/api/workspace-settings`` so the frontend can hit a small, typed
endpoint rather than the generic entity router. The actual storage is
the ``workspace_settings`` table (see ``app.models.workspace_settings``)
read/written through ``workspace_settings_service``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db
from app.services import workspace_settings_service

router = APIRouter(prefix="/workspace-settings", tags=["workspace-settings"])


class WorkspaceSettingsPayload(BaseModel):
    auto_bind_all_datasources: Optional[bool] = None


@router.get("")
def get_settings(
    org_id: str = "default-org",
    app_id: str = "default-app",
    db: Session = Depends(get_db),
) -> dict:
    """Return the current workspace settings as a typed JSON object."""
    return {
        "auto_bind_all_datasources": workspace_settings_service.get_bool(
            db,
            workspace_settings_service.KEY_AUTO_BIND_ALL_DATASOURCES,
            org_id=org_id,
            app_id=app_id,
        ),
    }


@router.put("")
def update_settings(
    payload: WorkspaceSettingsPayload,
    org_id: str = "default-org",
    app_id: str = "default-app",
    db: Session = Depends(get_db),
) -> dict:
    """Upsert the workspace settings. Only fields explicitly set in
    the body are updated.
    """
    if payload.auto_bind_all_datasources is not None:
        workspace_settings_service.set_value(
            db,
            workspace_settings_service.KEY_AUTO_BIND_ALL_DATASOURCES,
            "true" if payload.auto_bind_all_datasources else "false",
            org_id=org_id,
            app_id=app_id,
        )
        db.commit()
    return get_settings(org_id=org_id, app_id=app_id, db=db)
