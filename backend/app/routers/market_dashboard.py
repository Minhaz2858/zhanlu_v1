"""EDIA market dashboard — read-only mirror router.

All routes require a logged-in user (Depends(get_current_user_required)).
Fail-closed: empty EDIA_MYSQL_URL → 503 edia_unavailable; unreachable host
→ 503 edia_unreachable. No silent empty data.

Exception: GET /enabled is informational (returns 200 with {enabled: false}
when EDIA is not configured) so the sidebar can decide whether to render
the accordion.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import get_current_user_required
from app.schemas.market_dashboard import (
    EnabledResponse, MarketSnapshot, PriceHistoryPoint,
    RegionalPriceData, NewsListResponse,
    RegionalBatchResponse, ForecastBaseBatchResponse,
    EventsResponse, FxRateResponse, OverviewBundle,
    ForecastChartResponse, PriceChainResponse,
    SkuForecastMeta, DecisionBoardResponse,
)
from app.services import market_dashboard_service as svc
from app.services import decision_board_service as dbs

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


@router.get("/regional/{product_id}", response_model=RegionalPriceData)
def get_regional(
    product_id: str,
    user=Depends(get_current_user_required),
) -> RegionalPriceData:
    """Return per-region prices for a product (v1 simplified)."""
    return svc.get_regional_prices(product_id)


@router.get("/news", response_model=NewsListResponse)
def get_news(
    group: Optional[str] = Query(None, pattern="^(upstream|midstream|downstream)$"),
    limit: int = Query(18, ge=1, le=50),
    user=Depends(get_current_user_required),
) -> NewsListResponse:
    """Return industry news (v1: empty list with explanatory note)."""
    return svc.get_news_events(group=group, limit=limit)


# ── Phase 7 (verbatim port): batch + new endpoints ─────────────────


@router.get("/regional-batch", response_model=RegionalBatchResponse)
def get_regional_batch(
    product_ids: str = Query(..., description="comma-separated product ids"),
    user=Depends(get_current_user_required),
) -> RegionalBatchResponse:
    """Return regional prices for multiple products in one request."""
    ids = [p.strip() for p in product_ids.split(",") if p.strip()]
    return svc.get_regional_prices_batch(ids)


@router.get("/forecast-base-batch", response_model=ForecastBaseBatchResponse)
def get_forecast_base_batch(
    product_ids: str = Query(..., description="comma-separated product ids"),
    day: int = Query(7, ge=1, le=30),
    user=Depends(get_current_user_required),
) -> ForecastBaseBatchResponse:
    """Return 7-day-ahead forecast base prices for the requested products."""
    ids = [p.strip() for p in product_ids.split(",") if p.strip()]
    return svc.get_forecast_base_batch_v2(ids, day=day)


@router.get("/decision-board", response_model=DecisionBoardResponse)
def get_decision_board(
    group: str = Query(..., pattern="^(upstream|midstream|downstream)$"),
    day: int = Query(7, ge=1, le=30),
    user=Depends(get_current_user_required),
) -> DecisionBoardResponse:
    """Plain-language weekly decision board for all products in a group."""
    return dbs.build_decision_board(group=group, day=day)


@router.get("/events/latest", response_model=EventsResponse)
def get_events_latest(
    hours: int = Query(168, ge=1, le=8760),
    review_status: str = Query("approved"),
    min_usefulness_score: int = Query(0, ge=0, le=100),
    user=Depends(get_current_user_required),
) -> EventsResponse:
    """Return latest intelligence events (news) from the warehouse."""
    return svc.get_events_latest(
        hours=hours, review_status=review_status,
        min_usefulness_score=min_usefulness_score,
    )


@router.get("/fx/usd-cny", response_model=FxRateResponse)
def get_fx(
    user=Depends(get_current_user_required),
) -> FxRateResponse:
    """Return the USD-CNY FX rate (v1: static 7.20)."""
    return svc.get_fx_rate()


@router.get("/overview", response_model=OverviewBundle)
def get_overview(
    no_cache: bool = Query(False, description="Bypass the in-memory cache."),
    forecast_day: int = Query(7, ge=1, le=30),
    user=Depends(get_current_user_required),
) -> OverviewBundle:
    """One-shot payload (snapshot + regional + forecast + fx) for the
    Chemical Market Overview page. 30-second in-memory cache is shared
    across requests so the dashboard doesn't hammer the EDIA MySQL
    warehouse. Pass `?no_cache=1` (or call invalidate endpoint) to force
    a fresh fetch — used by the Refresh button in the UI.
    """
    return svc.get_overview_bundle(
        bypass_cache=no_cache, forecast_day=forecast_day,
    )


@router.post("/overview/refresh", response_model=dict)
def post_overview_refresh(
    user=Depends(get_current_user_required),
) -> dict:
    """Drop the in-memory overview cache (e.g. after a DB write triggers
    the frontend to invalidate caches globally)."""
    dropped = svc.invalidate_overview_cache()
    return {"invalidated": dropped, "key_prefix": "overview:bundle:"}


# ── Upstream page: forecast chart + price chain ──────────────────────


@router.get("/forecast-chart/{product_id}", response_model=ForecastChartResponse)
def get_forecast_chart(
    product_id: str,
    days: int = Query(30, ge=1, le=365),
    user=Depends(get_current_user_required),
) -> ForecastChartResponse:
    """Return merged past+forecast timeline for a single product.

    - Past zone (left): actual price + previous AI rec for accuracy review
    - Forecast zone (right): base / bull / bear curves from latest ForecastRun
    """
    return svc.get_forecast_chart(product_id, days=days)


@router.get("/forecast-skus")
def list_forecast_skus(
    product_id: str,
    user=Depends(get_current_user_required),
):
    """List all SKU-level forecast targets for an ERP product family.

    Returns material_code, target_id, report_order, and forecast data
    for each SKU (material_code) in the family.
    """
    from app.models.forecasting import ForecastTarget, ForecastRun
    from app.database import SessionLocal

    if product_id not in ("c5_resin", "raffinate_c5"):
        raise HTTPException(
            status_code=400,
            detail=f"product_id must be an ERP product ('c5_resin' or 'raffinate_c5'), got '{product_id}'"
        )

    db = SessionLocal()
    try:
        prefix = f"ecisco.{product_id}."
        targets = db.query(ForecastTarget).filter(
            ForecastTarget.product_key.like(f"{prefix}%"),
            ForecastTarget.org_id == "default-org",
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).order_by(ForecastTarget.report_order.asc()).all()

        skus = []
        for tgt in targets:
            material_code = tgt.product_key[len(prefix):]
            # Get latest forecast run for this SKU
            run = db.query(ForecastRun).filter(
                ForecastRun.target_id == tgt.id,
                ForecastRun.is_deleted == False,  # noqa: E712
            ).order_by(ForecastRun.created_date.desc()).first()

            sku_base = None
            sku_confidence = None
            if run and run.results:
                horizon_payload = run.results.get("7d") or {}
                base = horizon_payload.get("base")
                if isinstance(base, list) and len(base) > 0:
                    sku_base = float(base[0])
                elif isinstance(base, (int, float)):
                    sku_base = float(base)
                sku_confidence = run.confidence

            skus.append(SkuForecastMeta(
                material_code=material_code,
                target_id=str(tgt.id),
                name=tgt.name,
                report_order=tgt.report_order or 99,
                is_primary=(tgt.report_order is not None and tgt.report_order <= 12),
                forecast_base=sku_base,
                forecast_confidence=sku_confidence,
            ))

        return {"skus": [s.model_dump() for s in skus], "count": len(skus)}
    finally:
        db.close()


@router.get("/price-chain", response_model=PriceChainResponse)
def get_price_chain(
    user=Depends(get_current_user_required),
) -> PriceChainResponse:
    """Return 3-tier upstream price transmission chain.

    Each node: current price, 30d change, sparkline (30-day), percentile.
    Tiers: crude → naphtha → cracked C5/C9 → isoprene/piperylene/DCPD.
    """
    return svc.get_price_chain()
