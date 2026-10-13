"""Domain packs router — packaged vertical value (IBM Gap 5).

List is available to every authenticated user (it's the marketplace
browsing surface); install is Org-Admin (it changes the tenant's
glossary).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required, require_org_admin
from app.models.user import User
from app.services.domain_packs import install_pack, list_packs

router = APIRouter(tags=["domain-packs"])


@router.get("/domain-packs")
def get_domain_packs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Available industry packs + this org's install state."""
    return list_packs(db, org_id=user.org_id)


@router.post("/domain-packs/{pack_key}/install")
def install_domain_pack(
    pack_key: str,
    app_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Install an industry pack for the admin's org (idempotent)."""
    try:
        return install_pack(
            db,
            org_id=admin.org_id,
            app_id=app_id or admin.app_id or "default-app",
            pack_key=pack_key,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"pack install failed: {exc}")
