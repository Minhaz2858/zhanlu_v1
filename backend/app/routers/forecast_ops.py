"""Forecast MLOps ops endpoints: drift/adjustment status + manual evaluation."""
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException

from app.database import get_db
from app.deps import require_admin, get_current_user_required
from app.models.forecasting import ForecastTarget, ForecastWeightAdjustment
from app.services.forecasting.ops.evaluation_job import run_evaluation
from app.services.forecasting.features.threshold_calibrator import calibrate_thresholds

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
    degraded_count = (
        db.query(ForecastWeightAdjustment)
        .filter(
            ForecastWeightAdjustment.triggered_by == "accuracy_degradation",
            ForecastWeightAdjustment.applied == False,  # noqa: E712
        )
        .count()
    )
    return {
        "eval_enabled": _eval_enabled(),
        "degraded_count": degraded_count,
        "products": products,
    }


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


@router.post("/calibrate-thresholds")
def calibrate_decision_thresholds(
    body: dict = Body(default={}),
    db=Depends(get_db),
    user=Depends(require_admin),
):
    """Run grid-search threshold calibration from historical decision logs.

    Admin-only.  Query parameters (JSON body):
        product_key: str, optional  — filter to one product
        days: int, default 90       — lookback window
        buy_range: [float, float]   — optional grid range for buy threshold
        sell_range: [float, float]  — optional grid range for sell threshold
        min_change: float, default 0.03

    Returns advisory CalibrationReport — thresholds are NOT auto-applied.
    Apply manually via .env: FORECAST_BUY_THRESHOLD / FORECAST_SELL_THRESHOLD /
    FORECAST_EDGE_THRESHOLD after review.
    """
    product_key = body.get("product_key")
    days = int(body.get("days", 90))
    buy_range = body.get("buy_range")
    sell_range = body.get("sell_range")
    min_change = float(body.get("min_change", 0.03))

    if buy_range and len(buy_range) == 2:
        buy_range = tuple(buy_range)
    else:
        buy_range = None
    if sell_range and len(sell_range) == 2:
        sell_range = tuple(sell_range)
    else:
        sell_range = None

    report = calibrate_thresholds(
        db,
        product_key=product_key,
        days=days,
        buy_range=buy_range,
        sell_range=sell_range,
        min_change=min_change,
    )

    return {
        "product_key": report.product_key,
        "sample_size": report.sample_size,
        "date_range": report.date_range,
        "top_results": report.top_results,
        "recommendation": report.recommendation,
        "safety_checks": report.safety_checks,
        "warnings": report.warnings,
        "note": (
            "Thresholds are advisory only. Apply manually via "
            ".env FORECAST_BUY_THRESHOLD / FORECAST_SELL_THRESHOLD / "
            "FORECAST_EDGE_THRESHOLD after review."
        ),
    }


@router.get("/accuracy-flags")
def get_accuracy_flags(
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """List products flagged for accuracy degradation (T2.3).

    Admin-only view. Returns pending ForecastWeightAdjustment rows with
    triggered_by="accuracy_degradation"."""
    flags = (
        db.query(ForecastWeightAdjustment)
        .filter(
            ForecastWeightAdjustment.triggered_by == "accuracy_degradation",
            ForecastWeightAdjustment.applied == False,  # noqa: E712
        )
        .order_by(ForecastWeightAdjustment.created_date.desc())
        .all()
    )

    items = []
    for f in flags:
        target = db.query(ForecastTarget).filter(
            ForecastTarget.id == f.target_id
        ).first()
        items.append({
            "product_key": target.product_key if target else "unknown",
            "reason": f.reason,
            "old_weights": f.old_weights,
            "new_weights": f.new_weights,
            "delta_ratio": f.delta_ratio,
            "created_date": f.created_date.isoformat() if f.created_date else None,
        })

    return {"degraded_count": len(items), "flags": items}


@router.get("/threshold-config")
def get_threshold_config(
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """List all threshold configs (active + staged)."""
    configs = (
        db.query(ForecastThresholdConfig)
        .order_by(
            ForecastThresholdConfig.status.asc(),
            ForecastThresholdConfig.applied_at.desc(),
        )
        .all()
    )
    return {
        "configs": [
            {
                "product_key": c.product_key,
                "buy_threshold": c.buy_threshold,
                "sell_threshold": c.sell_threshold,
                "buy_min_change": c.buy_min_change,
                "edge_threshold": c.edge_threshold,
                "source": c.source,
                "status": c.status,
                "applied_at": c.applied_at.isoformat() if c.applied_at else None,
            }
            for c in configs
        ],
    }


@router.post("/run-autotune")
def run_autotune(
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Run threshold auto-tune (stages results; does NOT auto-apply)."""
    from app.services.forecasting.ops.threshold_auto_tuner import (
        run_threshold_autotune,
    )
    result = run_threshold_autotune(db)
    return result


@router.post("/apply-thresholds")
def apply_thresholds(
    product_key: str | None = None,
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Promote staged threshold configs to active for a product (or global).

    If product_key is None, promotes global staged configs.
    If product_key is provided, promotes only that product's staged configs.
    Previous active configs for the same scope are deactivated.
    """
    # Deactivate existing active configs for this scope
    existing = db.query(ForecastThresholdConfig).filter(
        ForecastThresholdConfig.status == "active",
    )
    if product_key is not None:
        existing = existing.filter(
            ForecastThresholdConfig.product_key == product_key,
        )
    else:
        existing = existing.filter(
            ForecastThresholdConfig.product_key.is_(None),
        )
    for row in existing.all():
        row.status = "inactive"
        db.flush()

    # Promote staged → active
    staged_query = db.query(ForecastThresholdConfig).filter(
        ForecastThresholdConfig.status == "staged",
    )
    if product_key is not None:
        staged_query = staged_query.filter(
            ForecastThresholdConfig.product_key == product_key,
        )
    else:
        staged_query = staged_query.filter(
            ForecastThresholdConfig.product_key.is_(None),
        )

    now = datetime.utcnow()
    count = 0
    for row in staged_query.order_by(
        ForecastThresholdConfig.created_date.desc()
    ).all():
        row.status = "active"
        row.source = f"{row.source} (applied)"
        row.applied_at = now
        count += 1

    db.flush()
    return {"applied": count}

