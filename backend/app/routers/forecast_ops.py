"""Forecast MLOps ops endpoints: drift/adjustment status + manual evaluation."""
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.database import get_db
from app.deps import require_admin, get_current_user_required
from app.models.forecasting import ForecastTarget, ForecastWeightAdjustment
from app.services.forecasting.ops.evaluation_job import run_evaluation

router = APIRouter(prefix="/forecast-ops", tags=["forecast-ops"])


def _eval_enabled() -> bool:
    return os.environ.get("FORECAST_EVAL_JOB_ENABLED", "false").lower() in ("1", "true", "yes")


@router.get("/status")
def get_status(
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Per-product drift/adjustment chips (last 7 days) + whether eval is on."""
    cutoff = datetime.utcnow() - timedelta(days=7)
    targets = db.query(ForecastTarget).filter(
        ForecastTarget.is_deleted == False  # noqa: E712
    ).all()
    products = []
    for t in targets:
        latest = db.query(ForecastWeightAdjustment).filter(
            ForecastWeightAdjustment.target_id == t.id,
            ForecastWeightAdjustment.created_date >= cutoff,
        ).order_by(ForecastWeightAdjustment.created_date.desc()).first()
        products.append({
            "product_key": t.product_key,
            "drifting": bool(latest and latest.triggered_by == "drift" and not latest.applied),
            "last_adjusted_at": (latest.created_date.isoformat() if latest else None),
            "last_trigger": (latest.triggered_by if latest else None),
        })
    return {"eval_enabled": _eval_enabled(), "products": products}


@router.post("/run-evaluation")
def trigger_evaluation(
    db=Depends(get_db),
    user=Depends(require_admin),
):
    """Manually trigger the evaluation job (admin-only). Closes the realized loop."""
    if not _eval_enabled():
        raise HTTPException(status_code=503, detail="forecast_eval_job_disabled")
    summary = run_evaluation(db)
    return summary
