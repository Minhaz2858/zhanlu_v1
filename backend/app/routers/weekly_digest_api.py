"""Weekly Summary & Forecast Digest API endpoints.

All endpoints are read-only except the manual trigger.
Gated by WEEKLY_DIGEST_ENABLED setting.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_current_user_required, get_db
from app.models.weekly_digest import WeeklyReportCard, WeeklyReportProduct
from app.schemas.weekly_digest import (
    WeeklyReportCardResponse,
    WeeklyReportDetailResponse,
    WeeklyReportListResponse,
    WeeklyReportProductResponse,
    WeeklyDigestTriggerResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/weekly-digest", tags=["weekly-digest"])


def _check_enabled():
    """Raise 404 if the feature is not enabled."""
    if not settings.WEEKLY_DIGEST_ENABLED:
        raise HTTPException(status_code=404, detail="Weekly digest feature is not enabled")


def _surface(db: Session):
    """Return the unified facade when the flag is on, else None.

    The legacy read path queries ``WeeklyReportCard``/``WeeklyReportProduct``
    directly in the router body. When ``None`` is returned, callers fall back
    to those inline queries.
    """
    if settings.UNIFIED_FORECASTING_SERVICE_ENABLED:
        from app.services.forecasting.unified_service import ForecastingAgentService
        return ForecastingAgentService(db)
    return None


# ── List reports ────────────────────────────────────────────────────────

@router.get("", response_model=WeeklyReportListResponse)
async def list_reports(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    """List all weekly report cards, newest first."""
    _check_enabled()
    org_id = getattr(user, "org_id", "default-org") or "default-org"
    s = _surface(db)
    if s is not None:
        res = s.get_weekly_digest_list(org_id=org_id, page=page, page_size=page_size)
        return WeeklyReportListResponse(
            items=[WeeklyReportCardResponse.model_validate(item) for item in res["items"]],
            total=res["total"],
            page=res["page"],
            page_size=res["page_size"],
        )

    query = db.query(WeeklyReportCard).filter(
        WeeklyReportCard.org_id == org_id,
        WeeklyReportCard.is_deleted == False,  # noqa: E712
    ).order_by(WeeklyReportCard.week_start.desc())

    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()

    return WeeklyReportListResponse(
        items=[WeeklyReportCardResponse.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


# ── Latest report (convenience) ────────────────────────────────────────

@router.get("/latest", response_model=Optional[WeeklyReportDetailResponse])
async def get_latest_report(
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Get the most recent ready weekly report."""
    _check_enabled()
    org_id = getattr(user, "org_id", "default-org") or "default-org"
    s = _surface(db)
    if s is not None:
        res = s.get_weekly_digest_latest(org_id=org_id)
        if res is None:
            return None
        return WeeklyReportDetailResponse(
            **WeeklyReportCardResponse.model_validate(res["card"]).model_dump(),
            products=[
                WeeklyReportProductResponse.model_validate(p) for p in res["products"]
            ],
        )

    card = db.query(WeeklyReportCard).filter(
        WeeklyReportCard.org_id == org_id,
        WeeklyReportCard.is_deleted == False,  # noqa: E712
        WeeklyReportCard.status == "ready",
    ).order_by(WeeklyReportCard.week_start.desc()).first()

    if not card:
        return None

    products = db.query(WeeklyReportProduct).filter(
        WeeklyReportProduct.report_id == card.id,
        WeeklyReportProduct.is_deleted == False,  # noqa: E712
    ).order_by(WeeklyReportProduct.report_order).all()

    return WeeklyReportDetailResponse(
        **WeeklyReportCardResponse.model_validate(card).model_dump(),
        products=[WeeklyReportProductResponse.model_validate(p) for p in products],
    )


# ── Get single report ──────────────────────────────────────────────────

@router.get("/{report_id}", response_model=WeeklyReportDetailResponse)
async def get_report(
    report_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Get a single weekly report with all product data."""
    _check_enabled()
    org_id = getattr(user, "org_id", "default-org") or "default-org"
    s = _surface(db)
    if s is not None:
        res = s.get_weekly_digest_detail(report_id=report_id, org_id=org_id)
        if res is None:
            raise HTTPException(status_code=404, detail="Report not found")
        return WeeklyReportDetailResponse(
            **WeeklyReportCardResponse.model_validate(res["card"]).model_dump(),
            products=[
                WeeklyReportProductResponse.model_validate(p) for p in res["products"]
            ],
        )

    card = db.query(WeeklyReportCard).filter(
        WeeklyReportCard.id == report_id,
        WeeklyReportCard.org_id == org_id,
        WeeklyReportCard.is_deleted == False,  # noqa: E712
    ).first()
    if not card:
        raise HTTPException(status_code=404, detail="Report not found")

    products = db.query(WeeklyReportProduct).filter(
        WeeklyReportProduct.report_id == report_id,
        WeeklyReportProduct.is_deleted == False,  # noqa: E712
    ).order_by(WeeklyReportProduct.report_order).all()

    return WeeklyReportDetailResponse(
        **WeeklyReportCardResponse.model_validate(card).model_dump(),
        products=[WeeklyReportProductResponse.model_validate(p) for p in products],
    )


# ── Export to PPTX ─────────────────────────────────────────────────────

_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@router.post("/{report_id}/export/pptx")
async def export_pptx(
    report_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Stream the weekly report as a branded ``.pptx`` deck."""
    _check_enabled()
    if not settings.WEEKLY_DIGEST_PPTX_EXPORT_ENABLED:
        raise HTTPException(status_code=503, detail="PPTX export is not enabled")

    org_id = getattr(user, "org_id", "default-org") or "default-org"
    card = db.query(WeeklyReportCard).filter(
        WeeklyReportCard.id == report_id,
        WeeklyReportCard.org_id == org_id,
        WeeklyReportCard.is_deleted == False,  # noqa: E712
    ).first()
    if not card:
        raise HTTPException(status_code=404, detail="Report not found")
    if card.status == "generating":
        raise HTTPException(status_code=503, detail="Report is being generated")

    from app.services.weekly_digest.service import build_pptx_for_report

    try:
        buf = build_pptx_for_report(db, report_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Report not found")
    except Exception as e:  # noqa: BLE001
        logger.exception("[weekly-digest] PPTX export failed for %s: %s", report_id, e)
        raise HTTPException(status_code=500, detail="Failed to generate PPTX")

    safe_week = re.sub(r"[^A-Za-z0-9_-]", "_", str(card.week_start))
    safe_org = re.sub(r"[^A-Za-z0-9_-]", "_", org_id)
    filename = f"weekly_digest_{safe_week}_{safe_org}.pptx"

    return StreamingResponse(
        buf,
        media_type=_PPTX_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Accel-Buffering": "no",
        },
    )


# ── Manual trigger ─────────────────────────────────────────────────────

@router.post("/trigger", response_model=WeeklyDigestTriggerResponse)
async def trigger_generation(
    background_tasks: BackgroundTasks,
    user=Depends(get_current_user_required),
):
    """Manually trigger weekly digest generation.

    Runs the generator in the background and returns immediately.
    """
    _check_enabled()
    org_id = getattr(user, "org_id", "default-org") or "default-org"

    def _run_sync():
        """Run the async generator in a new event loop."""
        import asyncio
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            if settings.UNIFIED_FORECASTING_SERVICE_ENABLED:
                from app.services.forecasting.unified_service import (
                    ForecastingAgentService,
                )

                gen = ForecastingAgentService(db).generate_weekly_digest_parallel(org_id=org_id)
            else:
                from app.services.weekly_digest.generator import (
                    generate_weekly_digest_parallel,
                )

                gen = generate_weekly_digest_parallel(db, org_id=org_id)

            loop = asyncio.new_event_loop()
            loop.run_until_complete(gen)
            loop.close()
        except Exception as e:
            logger.exception("[weekly-digest] Manual trigger generation failed: %s", e)
        finally:
            db.close()

    background_tasks.add_task(_run_sync)

    return WeeklyDigestTriggerResponse(
        status="triggered",
        message="Weekly digest generation has been triggered. Check back in a few minutes.",
    )


