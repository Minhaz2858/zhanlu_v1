"""ROI router — business-value metrics for the admin/CoE dashboard.

IBM enterprise-AI Gap 1 (2026-10-13): the platform records rich
operational data but had no business-value surface. Org Admins get their
tenant's ROI; platform admins get any org they ask for.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required, require_org_admin
from app.models.user import User
from app.services.roi_service import roi_summary

router = APIRouter(tags=["roi"])


@router.get("/roi/summary")
def get_roi_summary(
    days: int = Query(default=30, ge=1, le=365),
    org_id: str | None = Query(default=None),
    app_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """ROI summary for the caller's org (or another org for platform admins).

    Org Admins / Normal users are always scoped to their OWN org — the
    ``org_id`` query parameter is ignored for them (tenant isolation).
    Platform admins (role=admin on the default org) may pass org_id.
    """
    is_platform_admin = (
        user.role == "admin" and user.org_id == "default-org"
    )
    if org_id and is_platform_admin:
        effective_org = org_id
    elif org_id and org_id != user.org_id:
        # Non-platform callers may not probe other tenants — same 404
        # convention as the admin users surface (existence hidden).
        raise HTTPException(status_code=404, detail="Organization not found")
    else:
        effective_org = user.org_id

    return roi_summary(db, org_id=effective_org, days=days, app_id=app_id)
