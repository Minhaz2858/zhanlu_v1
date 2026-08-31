"""T2.3 Accuracy Feedback Loop — auto-flag products with degrading accuracy.

Scans recent decision logs per product, compares recent vs baseline accuracy,
and writes ForecastWeightAdjustment audit rows with retrain/refresh
recommendations when accuracy drops below threshold.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def run_accuracy_feedback(
    db: Session,
    recent_window_days: int = 30,
    baseline_days: int = 90,
    degradation_threshold_pct: float = 25.0,
) -> dict[str, Any]:
    """Auto-flag products whose decision accuracy has degraded.

    Per product:
      1. Compute recent decision accuracy (via roi_pct aggregate) over
         recent_window_days.
      2. Compare against baseline accuracy over the preceding baseline_days.
      3. If recent accuracy drops > degradation_threshold_pct (relative to
         baseline), write an audit row.

    Idempotent: skips products that already have a pending
    "accuracy_degradation" audit within the last 7 days.

    Returns: {checked, flagged, products: [dict]}
    """
    from app.models.forecasting import (
        ForecastDecisionLog,
        ForecastTarget,
        ForecastWeightAdjustment,
    )

    now = datetime.now(timezone.utc)
    recent_cutoff = (now - timedelta(days=recent_window_days)).date()
    baseline_cutoff_start = (now - timedelta(days=baseline_days + recent_window_days)).date()
    baseline_cutoff_end = (now - timedelta(days=recent_window_days)).date()
    pending_cutoff = now - timedelta(days=7)

    # Find active products with decision logs
    targets = (
        db.query(ForecastTarget)
        .filter(ForecastTarget.is_deleted == False)  # noqa: E712
        .all()
    )

    products: list[dict[str, Any]] = []
    flagged = 0

    for t in targets:
        pk = t.product_key

        # Idempotency: skip if pending audit within 7 days
        existing_audit = (
            db.query(ForecastWeightAdjustment)
            .filter(
                ForecastWeightAdjustment.target_id == t.id,
                ForecastWeightAdjustment.triggered_by == "accuracy_degradation",
                ForecastWeightAdjustment.created_date >= pending_cutoff,
            )
            .first()
        )
        if existing_audit is not None:
            continue

        # Recent ROI stats
        recent_rows = (
            db.query(ForecastDecisionLog)
            .filter(
                ForecastDecisionLog.product_id == pk,
                ForecastDecisionLog.roi_pct.isnot(None),
                ForecastDecisionLog.as_of_date >= recent_cutoff,
            )
            .all()
        )

        # Baseline ROI stats
        baseline_rows = (
            db.query(ForecastDecisionLog)
            .filter(
                ForecastDecisionLog.product_id == pk,
                ForecastDecisionLog.roi_pct.isnot(None),
                ForecastDecisionLog.as_of_date >= baseline_cutoff_start,
                ForecastDecisionLog.as_of_date < baseline_cutoff_end,
            )
            .all()
        )

        if not recent_rows or not baseline_rows:
            continue

        recent_roi_avg = sum(r.roi_pct for r in recent_rows) / len(recent_rows)
        baseline_roi_avg = sum(r.roi_pct for r in baseline_rows) / len(baseline_rows)

        if baseline_roi_avg == 0:
            continue

        # Relative degradation
        degradation_pct = (
            (baseline_roi_avg - recent_roi_avg) / abs(baseline_roi_avg) * 100.0
        )

        if degradation_pct > degradation_threshold_pct:
            reason_zh = (
                f"决策ROI从基线{baseline_roi_avg:.1f}%恶化至近期{recent_roi_avg:.1f}%"
                f"（降幅{degradation_pct:.0f}%），建议重新训练模型或刷新特征"
            )
            audit = ForecastWeightAdjustment(
                target_id=t.id,
                triggered_by="accuracy_degradation",
                reason=reason_zh,
                old_weights={
                    "baseline_roi_avg": round(baseline_roi_avg, 3),
                    "baseline_samples": len(baseline_rows),
                },
                new_weights={
                    "recent_roi_avg": round(recent_roi_avg, 3),
                    "recent_samples": len(recent_rows),
                    "degradation_pct": round(degradation_pct, 1),
                },
                delta_ratio=round(degradation_pct / 100.0, 4),
                applied=False,
                org_id=t.org_id,
            )
            db.add(audit)
            db.flush()
            flagged += 1

            products.append({
                "product_key": pk,
                "recent_roi_avg": round(recent_roi_avg, 3),
                "baseline_roi_avg": round(baseline_roi_avg, 3),
                "degradation_pct": round(degradation_pct, 1),
                "recommendation": "retrain_model",
            })

    return {
        "checked": len(targets),
        "flagged": flagged,
        "products": products,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Post-forecast accuracy threshold checking (MAPE-based iteration trigger)
# ═══════════════════════════════════════════════════════════════════════════

import numpy as np  # noqa: E402


def recommend_iteration(
    product_key: str,
    mape: float,
    trend: str = "stable",
) -> list[str]:
    """Generate specific iteration recommendations based on MAPE and trend.

    Args:
        product_key: The product identifier.
        mape: Latest realized MAPE (0-100 scale).
        trend: Trend direction: 'stable', 'improving', 'degrading'.

    Returns:
        List of actionable recommendation strings.
    """
    recs: list[str] = []

    if mape < 8.0:
        recs.append(f"[{product_key}] MAPE {mape:.1f}% — within excellent range. No action needed.")
        return recs

    if mape < 15.0:
        if trend == "degrading":
            recs.append(
                f"[{product_key}] MAPE {mape:.1f}% acceptable but trend is degrading. "
                f"Monitor closely; consider preemptive XGBoost re-tuning."
            )
        else:
            recs.append(
                f"[{product_key}] MAPE {mape:.1f}% — acceptable. Monitor for regression."
            )
        return recs

    # MAPE >= 15.0 — active action needed
    recs.append(f"[{product_key}] MAPE {mape:.1f}% — **action recommended:**")

    if mape >= 25.0:
        recs.append(f"  1. [CRITICAL] Set needs_rebuild and trigger full re-fit cycle.")
    else:
        recs.append(f"  1. Re-run XGBoost tuning: tune_xgboost_params('{product_key}')")

    recs.append(f"  2. Check ensemble weights — may need regime-aware pool adjustment.")
    recs.append(f"  3. Review feature freshness (technical indicators, Fourier, upstream market data).")

    if mape > 30.0:
        recs.append(f"  4. [EXTREME] Disable ML models, fall back to statistical baseline.")
        recs.append(f"  5. Investigate data pipeline — possible upstream data corruption.")

    recs.append(f"  6. Consider enabling stacking or VAR model if disabled.")

    return recs


def _estimate_trend(db: Session, target_id: int, days: int = 30) -> str:
    """Compare recent MAPE with older MAPE to determine trend."""
    from app.models.forecasting import ForecastAccuracyLog

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    mid = datetime.now(timezone.utc) - timedelta(days=days // 2)

    logs = (
        db.query(ForecastAccuracyLog)
        .filter(
            ForecastAccuracyLog.target_id == target_id,
            ForecastAccuracyLog.realized_mape.isnot(None),
            ForecastAccuracyLog.evaluated_at >= cutoff,
        )
        .order_by(ForecastAccuracyLog.evaluated_at.desc())
        .limit(20)
        .all()
    )

    if len(logs) < 3:
        return "stable"

    mid_idx = len(logs) // 2
    older_mape = np.mean([lg.realized_mape for lg in logs[mid_idx:]])
    newer_mape = np.mean([lg.realized_mape for lg in logs[:mid_idx]])

    delta = newer_mape - older_mape
    if delta < -2.0:
        return "improving"
    if delta > 2.0:
        return "degrading"
    return "stable"


def _flag_needs_rebuild(db: Session, target, status: str, mape: float | None, now: datetime) -> None:
    """Flag a ForecastTarget for rebuild when accuracy falls below thresholds."""
    logger.warning(
        "Flagging target %s (%s): status=%s, mape=%s — setting needs_rebuild",
        target.product_key, target.name, status, mape,
    )
    target.status = "needs_rebuild"
    target.model_config = target.model_config or {}
    target.model_config["accuracy_alert"] = {
        "status": status,
        "mape": mape,
        "flagged_at": now.isoformat(),
    }
    db.add(target)
    db.flush()


def check_accuracy_thresholds(
    db: Session,
    product_key: str | None = None,
    days: int = 30,
) -> dict[str, dict]:
    """Check each product's latest ForecastAccuracyLog against MAPE thresholds.

    For products in critical/blocked status, auto-flags ForecastTarget.status
    to 'needs_rebuild' so downstream jobs can act.

    Returns dict: {product_key: {status, mape, mae, rmse, trend, recommendations}}
    """
    from app.models.forecasting import ForecastTarget, ForecastAccuracyLog
    from app.services.forecasting.accuracy_report import AccuracyThreshold
    from sqlalchemy import func

    threshold = AccuracyThreshold()

    latest_eval = (
        db.query(
            ForecastAccuracyLog.target_id,
            func.max(ForecastAccuracyLog.evaluated_at).label("max_eval"),
        )
        .filter(ForecastAccuracyLog.realized_mape.isnot(None))
        .filter(ForecastAccuracyLog.horizon_days == 7)
        .group_by(ForecastAccuracyLog.target_id)
        .subquery()
    )

    query = (
        db.query(ForecastAccuracyLog, ForecastTarget)
        .join(latest_eval, ForecastAccuracyLog.target_id == latest_eval.c.target_id)
        .join(ForecastTarget, ForecastTarget.id == ForecastAccuracyLog.target_id)
        .filter(
            ForecastAccuracyLog.evaluated_at == latest_eval.c.max_eval,
            ForecastAccuracyLog.horizon_days == 7,
            ForecastTarget.is_deleted == False,
        )
    )

    if product_key:
        query = query.filter(ForecastTarget.product_key == product_key)

    results: dict[str, dict] = {}
    now = datetime.now(timezone.utc)

    for log, target in query.all():
        pk = target.product_key
        mape = log.realized_mape
        status = threshold.check(mape)
        trend = _estimate_trend(db, target.id, days)

        recommendations = recommend_iteration(pk, mape, trend)
        results[pk] = {
            "status": status,
            "mape": mape,
            "mae": log.mae,
            "rmse": log.rmse,
            "trend": trend,
            "target_id": target.id,
            "recommendations": recommendations,
        }

        if status in ("critical", "blocked"):
            _flag_needs_rebuild(db, target, status, mape, now)

    return results


# ---------------------------------------------------------------------------
# P2.16: Rebuild-queue accessor (for ops endpoints)
# ---------------------------------------------------------------------------

def needs_rebuild_products(session=None) -> list[dict]:
    """Return products flagged for rebuild.

    Queries ForecastTarget rows with status='needs_rebuild'.
    Returns list of dicts with product_key, name, and reason.
    """
    if session is None:
        return []

    try:
        from app.models.forecasting import ForecastTarget
        targets = session.query(ForecastTarget).filter(
            ForecastTarget.status == "needs_rebuild"
        ).all()
        return [
            {
                "product_key": t.product_key,
                "name": t.name,
                "reason": (t.model_config or {}).get("accuracy_alert", {}),
            }
            for t in targets
        ]
    except Exception as exc:
        logger.warning("needs_rebuild_products query failed: %s", exc)
        return []
