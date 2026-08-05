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

    now = datetime.utcnow()
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
