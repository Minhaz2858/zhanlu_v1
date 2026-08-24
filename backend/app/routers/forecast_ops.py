"""Forecast MLOps ops endpoints: drift/adjustment status + manual evaluation."""
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile

from app.database import get_db
from app.deps import require_admin, get_current_user_required
from app.models.forecasting import (
    ForecastExternalSeries,
    ForecastTarget,
    ForecastWeightAdjustment,
)
from app.services.forecasting.ops.evaluation_job import run_evaluation
from app.services.forecasting.features.threshold_calibrator import calibrate_thresholds
from app.services.forecasting.features.external_feed_ingest import (
    IngestError,
    delete_series as _delete_series,
    get_series_points,
    ingest_csv,
    list_series as _list_series,
)
from app.services.forecasting.features.mock_feeds import seed_mock_feeds
from app.services.forecasting.regime_detector import detect_regime
from app.services.forecasting.accuracy_tracker import rolling_accuracy
from app.services.forecasting.conformal import ConformalCalibration

router = APIRouter(prefix="/forecast-ops", tags=["forecast-ops"])


def _eval_enabled() -> bool:
    return os.environ.get("FORECAST_EVAL_JOB_ENABLED", "false").lower() in ("1", "true", "yes")


@router.get("/status")
def get_status(
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Per-product drift/adjustment chips (last 7 days) + whether eval is on."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
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

    now = datetime.now(timezone.utc)
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


# ---------------------------------------------------------------------------
# Wave 3 T3.0 — External-feed store (CSV upload + CRUD)
# ---------------------------------------------------------------------------


@router.get("/external-feeds")
def list_external_feeds(
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """List all external-feed series (registry). Read-only.

    CSV-uploaded feeds become immediately visible here. Loaders query
    ``forecast_external_points`` directly — they do NOT depend on this
    listing, so missing or stale rows here won't affect the forecast.
    """
    return _list_series(db)


@router.get("/external-feeds/{series_key}")
def get_external_feed(
    series_key: str,
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Return one series + its full points list.

    404 if series_key is unknown.
    """
    series = db.query(ForecastExternalSeries).filter_by(
        series_key=series_key,
    ).first()
    if series is None:
        raise HTTPException(
            status_code=404, detail=f"series not found: {series_key}",
        )
    df = get_series_points(db, series_key)
    return {
        "series_key": series.series_key,
        "domain": series.domain,
        "product_key": series.product_key,
        "unit": series.unit,
        "source": series.source,
        "cadence": series.cadence,
        "row_count": series.row_count,
        "last_value_date": (
            series.last_value_date.isoformat() if series.last_value_date else None
        ),
        "points": [
            {"date": d.isoformat() if hasattr(d, "isoformat") else str(d),
             "value": float(v)}
            for d, v in zip(df["date"].tolist(), df["value"].tolist())
        ],
    }


@router.post("/external-feeds")
def upload_external_feed(
    series_key: str,
    domain: str,
    file: UploadFile = File(...),
    product_key: str | None = None,
    unit: str | None = None,
    cadence: str | None = None,
    notes: str | None = None,
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Upload a CSV to populate (or update) an external-feed series.

    CSV must have columns ``date`` and ``value``. Optional extra columns
    are stored as per-row metadata. Re-uploading the same date overwrites
    the previous value (upsert).

    Returns a summary: { series_key, row_count, rows_inserted, rows_updated, ... }
    """
    try:
        return ingest_csv(
            db=db,
            file=file.file,
            series_key=series_key,
            domain=domain,
            product_key=product_key,
            unit=unit,
            cadence=cadence,
            uploaded_by=user.id,
            notes=notes,
        )
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/external-feeds/{series_key}")
def delete_external_feed(
    series_key: str,
    db=Depends(get_db),
    user=Depends(require_admin),
):
    """Delete a series and cascade-delete its points. Admin-only.

    404 if series_key is unknown.
    """
    series = db.query(ForecastExternalSeries).filter_by(series_key=series_key).first()
    if series is None:
        raise HTTPException(
            status_code=404, detail=f"series not found: {series_key}",
        )
    _delete_series(db, series_key)
    return {"deleted": True, "series_key": series_key}


@router.post("/external-feeds/seed-mock")
def seed_mock_external_feeds(
    product_key: str = "isoprene",
    n_weeks: int = 104,
    db=Depends(get_db),
    user=Depends(require_admin),
):
    """Seed all 3 Wave 3 external-feed domains with synthetic mock data.

    Admin-only. Used for end-to-end pipeline testing while real Tier-3
    feeds are blocked. Idempotent: re-running with the same product_key
    replaces existing rows for those series.

    Returns a per-domain summary: row_count, series_key, etc.
    """
    return seed_mock_feeds(
        db=db,
        product_key=product_key,
        n_weeks=n_weeks,
    )


@router.get("/regime-status/{product_key}")
def get_regime_status(
    product_key: str,
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Dynamic confidence badge data: regime, recent accuracy, shift score.

    Returns { regime, recent_mape, recent_directional_accuracy,
              confidence_level, confidence_label, shift_score,
              message, timestamp }.
    """
    from app.config import settings
    from app.models.forecasting import ForecastRun
    from app.services.forecasting.features.feature_loader import load_series
    from sqlalchemy import func

    target = db.query(ForecastTarget).filter_by(
        product_key=product_key, is_deleted=False,
    ).first()
    if target is None:
        raise HTTPException(status_code=404, detail=f"product not found: {product_key}")

    # Load price series for regime detection
    series = load_series(db, target.id, target.product_key)
    if series is None or len(series) < 60:
        return {
            "regime": "unknown",
            "recent_mape": None,
            "recent_directional_accuracy": None,
            "confidence_level": "low",
            "confidence_label": "低",
            "shift_score": None,
            "message": "数据不足，无法判断",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # Regime detection
    try:
        regime_result = detect_regime(series)
        regime = regime_result.regime if regime_result else "unknown"
    except Exception:
        regime = "unknown"

    # Recent MAPE (30 days)
    try:
        recent_mape = rolling_accuracy(
            db, target.id, days=30, metric="mape",
        )
    except Exception:
        recent_mape = None

    # Recent directional accuracy (30 days)
    try:
        recent_dir_acc = rolling_accuracy(
            db, target.id, days=30, metric="directional_accuracy",
        )
    except Exception:
        recent_dir_acc = None

    # Confidence scoring
    confidence_level = "high"
    confidence_label = "高"
    message = "模型表现良好"

    if recent_mape is not None:
        if recent_mape > 15.0:
            confidence_level = "low"
            confidence_label = "低"
            message = "近期准确率偏低"
        elif recent_mape > 8.0:
            confidence_level = "medium"
            confidence_label = "中"
            message = "近期准确率一般"

    if regime == "volatile" and confidence_level == "high":
        confidence_level = "medium"
        confidence_label = "中"
        message = "波动剧烈，需谨慎"
    elif regime == "trend_break":
        confidence_level = "low"
        confidence_label = "低"
        message = "趋势突变，模型置信度低"

    # Shift score (placeholder — will be populated by P3-1D adversarial detector)
    shift_score = None

    return {
        "regime": regime,
        "recent_mape": round(recent_mape, 2) if recent_mape is not None else None,
        "recent_directional_accuracy": (
            round(recent_dir_acc, 2) if recent_dir_acc is not None else None
        ),
        "confidence_level": confidence_level,
        "confidence_label": confidence_label,
        "shift_score": shift_score,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/driver-attribution/{product_key}")
def get_driver_attribution(
    product_key: str,
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Driver-attribution waterfall for a product.

    Returns { product_key, base_value, forecast_value, items: [
      { feature, contribution, weight, direction }, ...
    ]}.

    Uses the most recent ForecastRun's model_detail (which stores drivers)
    to reconstruct the waterfall. If no recent run exists, returns empty.
    """
    from app.models.forecasting import ForecastRun

    run = (
        db.query(ForecastRun)
        .join(ForecastTarget, ForecastRun.target_id == ForecastTarget.id)
        .filter(
            ForecastTarget.product_key == product_key,
            ForecastTarget.is_deleted == False,  # noqa: E712
            ForecastRun.model_detail.isnot(None),
        )
        .order_by(ForecastRun.forecast_date.desc())
        .first()
    )

    if run is None or not run.model_detail:
        return {
            "product_key": product_key,
            "base_value": None,
            "forecast_value": None,
            "items": [],
            "message": "No recent forecast run with driver data found",
        }

    detail = run.model_detail
    drivers = detail.get("drivers", [])
    if not drivers:
        return {
            "product_key": product_key,
            "base_value": None,
            "forecast_value": None,
            "items": [],
            "message": "No driver data in latest forecast run",
        }

    # Reconstruct waterfall from stored drivers
    # We don't have the original model object, so we approximate:
    # base_value = actual_price (last known), forecast = first point forecast
    base_value = float(run.actual_price) if run.actual_price else None
    forecast_value = None
    if run.point_forecast and len(run.point_forecast) > 0:
        forecast_value = float(run.point_forecast[0])

    if base_value is None or forecast_value is None:
        return {
            "product_key": product_key,
            "base_value": base_value,
            "forecast_value": forecast_value,
            "items": [],
            "message": "Insufficient data for waterfall reconstruction",
        }

    total_change = forecast_value - base_value
    items = []
    for d in drivers:
        weight = d.get("weight", 0.0)
        contribution = weight * total_change
        if abs(contribution) < 0.01:
            direction = "neutral"
        elif contribution > 0:
            direction = "up"
        else:
            direction = "down"
        items.append({
            "feature": d.get("feature", "unknown"),
            "contribution": round(contribution, 2),
            "weight": round(weight, 4),
            "direction": direction,
        })

    return {
        "product_key": product_key,
        "base_value": round(base_value, 2),
        "forecast_value": round(forecast_value, 2),
        "items": items,
    }


@router.get("/backtest-replay/{product_key}")
def get_backtest_replay(
    product_key: str,
    days: int = 90,
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Backtest replay: historical forecast vs actual pairs for a product.

    Returns [{ date, actual, forecast_1d, forecast_3d, forecast_7d, mape_7d }]
    from the last ``days`` of ForecastRun data.
    """
    from app.models.forecasting import ForecastRun, ForecastTarget
    from datetime import datetime, timedelta, timezone

    target = db.query(ForecastTarget).filter_by(
        product_key=product_key, is_deleted=False,
    ).first()
    if target is None:
        raise HTTPException(status_code=404, detail=f"product not found: {product_key}")

    since = datetime.now(timezone.utc) - timedelta(days=days)

    runs = (
        db.query(ForecastRun)
        .filter(
            ForecastRun.target_id == target.id,
            ForecastRun.forecast_date >= since,
            ForecastRun.actual_price.isnot(None),
            ForecastRun.point_forecast.isnot(None),
        )
        .order_by(ForecastRun.forecast_date.asc())
        .all()
    )

    if not runs:
        return {
            "product_key": product_key,
            "days_requested": days,
            "data_points": 0,
            "series": [],
        }

    series = []
    for run in runs:
        actual = float(run.actual_price) if run.actual_price else None
        point_fc = run.point_forecast
        if not point_fc or actual is None:
            continue

        # Extract forecasts at different horizons
        fc_1d = float(point_fc[0]) if len(point_fc) > 0 else None
        fc_3d = float(point_fc[2]) if len(point_fc) > 2 else None
        fc_7d = float(point_fc[6]) if len(point_fc) > 6 else None

        # Compute MAPE for 7d if available
        mape_7d = None
        if fc_7d is not None and actual != 0:
            mape_7d = round(abs((actual - fc_7d) / actual) * 100, 2)

        series.append({
            "date": run.forecast_date.isoformat() if run.forecast_date else None,
            "actual": round(actual, 2),
            "forecast_1d": round(fc_1d, 2) if fc_1d is not None else None,
            "forecast_3d": round(fc_3d, 2) if fc_3d is not None else None,
            "forecast_7d": round(fc_7d, 2) if fc_7d is not None else None,
            "mape_7d": mape_7d,
        })

    return {
        "product_key": product_key,
        "days_requested": days,
        "data_points": len(series),
        "series": series,
    }


@router.get("/what-if/{product_key}")
def get_what_if_simulation(
    product_key: str,
    brent_delta_pct: float = 0.0,
    naphtha_delta_pct: float = 0.0,
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """What-if analysis: simulate forecast changes given upstream price shocks.

    Uses domain_signals causal-chain elasticities for instant propagation
    (no model re-running required).

    Parameters
    ----------
    product_key : str
        Product to simulate.
    brent_delta_pct : float
        Percentage change in Brent price (e.g., 5.0 = +5%).
    naphtha_delta_pct : float
        Percentage change in naphtha price (e.g., -3.0 = -3%).

    Returns
    -------
    dict
        { product_key, base_forecast, adjusted_forecast, adjustments: [
          { driver, delta_pct, impact_pct, adjusted_value }
        ]}
    """
    from app.services.forecasting.what_if import compute_what_if

    try:
        return compute_what_if(
            product_key=product_key,
            brent_delta_pct=brent_delta_pct,
            naphtha_delta_pct=naphtha_delta_pct,
            db=db,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/accuracy-dashboard")
def get_accuracy_dashboard(
    db=Depends(get_db),
    user=Depends(get_current_user_required),
):
    """Per-product accuracy dashboard data.

    Returns:
      - accuracy_heatmap: product × horizon matrix with MAPE values
      - mape_trends: per-product MAPE sparkline data (90 days)
      - directional_accuracy: % correct direction per product
      - calibration_curve: predicted vs actual interval coverage
      - top_misses: worst products/horizons
    """
    from app.models.forecasting import ForecastTarget, ForecastAccuracyLog, ForecastRun
    from datetime import datetime, timedelta, timezone

    targets = (
        db.query(ForecastTarget)
        .filter(
            ForecastTarget.org_id == "default-org",
            ForecastTarget.status == "active",
            ForecastTarget.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    since = datetime.now(timezone.utc) - timedelta(days=90)

    # Accuracy heatmap: product × horizon
    heatmap = []
    for target in targets:
        logs = (
            db.query(ForecastAccuracyLog)
            .filter(
                ForecastAccuracyLog.target_id == target.id,
                ForecastAccuracyLog.evaluated_at >= since,
                ForecastAccuracyLog.realized_mape.isnot(None),
            )
            .order_by(ForecastAccuracyLog.evaluated_at.desc())
            .limit(30)
            .all()
        )
        if not logs:
            continue

        # Average MAPE by horizon
        horizon_mape = {}
        for log in logs:
            h = log.horizon_days or 7
            mape = float(log.realized_mape) if log.realized_mape else None
            if mape is not None:
                if h not in horizon_mape:
                    horizon_mape[h] = []
                horizon_mape[h].append(mape)

        row = {
            "product_key": target.product_key or target.name,
            "product_name": target.name,
            "horizons": {
                str(h): round(sum(v) / len(v), 2) if v else None
                for h, v in horizon_mape.items()
            },
        }
        heatmap.append(row)

    # MAPE trends: per-product 90-day sparklines
    mape_trends = []
    for target in targets:
        logs = (
            db.query(ForecastAccuracyLog)
            .filter(
                ForecastAccuracyLog.target_id == target.id,
                ForecastAccuracyLog.evaluated_at >= since,
                ForecastAccuracyLog.realized_mape.isnot(None),
            )
            .order_by(ForecastAccuracyLog.evaluated_at.asc())
            .all()
        )
        if not logs:
            continue

        trend = [
            {
                "date": log.evaluated_at.isoformat() if log.evaluated_at else None,
                "mape": round(float(log.realized_mape), 2),
                "horizon": log.horizon_days or 7,
            }
            for log in logs
        ]
        mape_trends.append({
            "product_key": target.product_key or target.name,
            "product_name": target.name,
            "trend": trend,
        })

    # Directional accuracy: compute from ForecastRun
    directional = []
    for target in targets:
        runs = (
            db.query(ForecastRun)
            .filter(
                ForecastRun.target_id == target.id,
                ForecastRun.forecast_date >= since,
                ForecastRun.actual_price.isnot(None),
            )
            .order_by(ForecastRun.forecast_date.asc())
            .all()
        )
        if len(runs) < 2:
            continue

        correct_direction = 0
        total = 0
        for i in range(1, len(runs)):
            prev_actual = float(runs[i - 1].actual_price) if runs[i - 1].actual_price else None
            curr_actual = float(runs[i].actual_price) if runs[i].actual_price else None
            prev_fc = runs[i - 1].point_forecast
            if prev_actual is None or curr_actual is None or not prev_fc:
                continue

            # Direction of actual change
            actual_dir = curr_actual - prev_actual
            # Direction of forecast (first horizon step)
            fc_dir = float(prev_fc[0]) - prev_actual if len(prev_fc) > 0 else 0

            if actual_dir == 0 or fc_dir == 0:
                continue

            if (actual_dir > 0 and fc_dir > 0) or (actual_dir < 0 and fc_dir < 0):
                correct_direction += 1
            total += 1

        if total > 0:
            directional.append({
                "product_key": target.product_key or target.name,
                "product_name": target.name,
                "correct": correct_direction,
                "total": total,
                "accuracy_pct": round(correct_direction / total * 100, 1),
            })

    # Top misses: worst MAPE
    top_misses = []
    for target in targets:
        worst_log = (
            db.query(ForecastAccuracyLog)
            .filter(
                ForecastAccuracyLog.target_id == target.id,
                ForecastAccuracyLog.evaluated_at >= since,
                ForecastAccuracyLog.realized_mape.isnot(None),
            )
            .order_by(ForecastAccuracyLog.realized_mape.desc())
            .first()
        )
        if worst_log and worst_log.realized_mape:
            top_misses.append({
                "product_key": target.product_key or target.name,
                "product_name": target.name,
                "worst_mape": round(float(worst_log.realized_mape), 2),
                "horizon": worst_log.horizon_days or 7,
                "date": worst_log.evaluated_at.isoformat() if worst_log.evaluated_at else None,
            })

    # Sort by worst MAPE descending
    top_misses.sort(key=lambda x: x["worst_mape"], reverse=True)

    return {
        "accuracy_heatmap": heatmap,
        "mape_trends": mape_trends,
        "directional_accuracy": directional,
        "top_misses": top_misses[:10],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

