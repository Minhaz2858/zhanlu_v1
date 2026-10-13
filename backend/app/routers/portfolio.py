"""Portfolio router — the AI-CoE use-case board (IBM Gap 2).

Org-scoped like every admin surface: Org Admins manage THEIR portfolio;
platform admins may query another org explicitly. Writes are Org-Admin
only (ownership/KPI/stage are governance decisions).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user_required, require_org_admin
from app.models.user import User
from app.services.usecase_portfolio import get_portfolio, upsert_entry

router = APIRouter(tags=["portfolio"])


@router.get("/portfolio")
def get_usecase_portfolio(
    days: int = Query(default=30, ge=1, le=365),
    org_id: str | None = Query(default=None),
    app_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """The CoE portfolio board — same org-scoping rules as /roi/summary."""
    is_platform_admin = user.role == "admin" and user.org_id == "default-org"
    if org_id and is_platform_admin:
        effective_org = org_id
    elif org_id and org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    else:
        effective_org = user.org_id
    return get_portfolio(db, org_id=effective_org, days=days, app_id=app_id)


class PortfolioUpdateBody(BaseModel):
    agent_id: str
    agent_name: str | None = None
    business_owner: str | None = None
    business_kpi: str | None = None
    lifecycle_stage: str | None = None
    value_notes: str | None = None


@router.put("/portfolio")
def update_usecase_portfolio(
    body: PortfolioUpdateBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_org_admin),
):
    """Set business owner / KPI / lifecycle stage for one agent use case."""
    try:
        entry = upsert_entry(
            db,
            org_id=admin.org_id,
            agent_id=body.agent_id,
            agent_name=body.agent_name,
            business_owner=body.business_owner,
            business_kpi=body.business_kpi,
            lifecycle_stage=body.lifecycle_stage,
            value_notes=body.value_notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return entry.to_dict()
