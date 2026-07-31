"""EDIA market dashboard — read-only mirror router.

All routes require a logged-in user (Depends(get_current_user_required)).
Fail-closed: empty EDIA_MYSQL_URL → 503 edia_unavailable; unreachable host
→ 503 edia_unreachable. No silent empty data.

Exception: GET /enabled is informational (returns 200 with {enabled: false}
when EDIA is not configured) so the sidebar can decide whether to render
the accordion.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.deps import get_current_user_required
from app.schemas.market_dashboard import (
    EnabledResponse, MarketSnapshot, PriceHistoryPoint,
)
from app.services import market_dashboard_service as svc

router = APIRouter(prefix="/market-dashboard", tags=["market-dashboard"])


@router.get("/enabled", response_model=EnabledResponse)
def get_enabled(user=Depends(get_current_user_required)) -> EnabledResponse:
    if not svc.is_edia_enabled():
        return EnabledResponse(enabled=False, reachable=False, reason="not_configured")
    reachable = svc.is_edia_reachable_cached()
    return EnabledResponse(
        enabled=True,
        reachable=reachable,
        reason="ok" if reachable else "configured_unreachable",
    )


@router.get("/snapshot", response_model=list[MarketSnapshot])
def get_snapshot(
    group: Optional[str] = Query(None, pattern="^(upstream|midstream|downstream)$"),
    user=Depends(get_current_user_required),
) -> list[MarketSnapshot]:
    """Return one snapshot per product in the requested group.

    group omitted → all 12 products (overview section).
    """
    return svc.get_market_snapshot(group=group)


@router.get("/history/{product_id}", response_model=list[PriceHistoryPoint])
def get_history(
    product_id: str,
    days: int = Query(30, ge=1, le=365),
    user=Depends(get_current_user_required),
) -> list[PriceHistoryPoint]:
    return svc.get_price_history(product_id, days=days)
