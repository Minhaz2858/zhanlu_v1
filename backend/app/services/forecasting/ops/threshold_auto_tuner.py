"""T2.4 Threshold Self-Calibration — auto-tune thresholds from realised ROI.

Calls calibrate_thresholds() per product with scored decision logs.
If guardrails pass (min samples, positive ROI, meaningful gap), writes
a STAGED ForecastThresholdConfig row. Does NOT auto-activate.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def run_threshold_autotune(
    db: Session,
    min_decisions: int = 30,
    min_accuracy: float = 0.45,
    gap_threshold: float = 0.15,
) -> dict[str, Any]:
    """Run threshold calibration per product and stage optimized thresholds.

    Guardrails (all must pass):
      - At least min_decisions scored logs for the product
      - Overall directional accuracy >= min_accuracy
      - Best ROI gap vs default >= gap_threshold

    Writes STAGED ForecastThresholdConfig rows; admin must promote to active.
    Returns {products_checked, staged, skipped, details}.
    """
    from app.models.forecasting import (
        ForecastDecisionLog,
        ForecastTarget,
        ForecastThresholdConfig,
    )
    from app.services.forecasting.features.threshold_calibrator import (
        calibrate_thresholds,
    )

    targets = (
        db.query(ForecastTarget)
        .filter(ForecastTarget.is_deleted == False)  # noqa: E712
        .filter(ForecastTarget.status == "active")
        .all()
    )

    products_checked = 0
    staged = 0
    skipped = 0
    details: list[dict[str, Any]] = []

    for t in targets:
        pk = t.product_key
        products_checked += 1

        # Count scored decision logs for this product
        scored = (
            db.query(ForecastDecisionLog)
            .filter(
                ForecastDecisionLog.product_id == pk,
                ForecastDecisionLog.roi_pct.isnot(None),
            )
            .count()
        )
        if scored < min_decisions:
            skipped += 1
            details.append({"product_key": pk, "reason": "insufficient_decisions", "scored": scored})
            continue

        # Guardrail: directional accuracy check (was declared at line 21, never enforced)
        scored_rows = (
            db.query(
                ForecastDecisionLog.predicted_p_rise,
                ForecastDecisionLog.actual_price_t,
                ForecastDecisionLog.actual_price_th,
            )
            .filter(
                ForecastDecisionLog.product_id == pk,
                ForecastDecisionLog.roi_pct.isnot(None),
                ForecastDecisionLog.actual_price_t.isnot(None),
                ForecastDecisionLog.actual_price_th.isnot(None),
                ForecastDecisionLog.predicted_p_rise.isnot(None),
            )
            .all()
        )
        if scored_rows:
            correct = sum(
                1 for row in scored_rows
                if (row.predicted_p_rise > 0.5) == (row.actual_price_th > row.actual_price_t)
            )
            dir_accuracy = correct / len(scored_rows)
            if dir_accuracy < min_accuracy:
                skipped += 1
                details.append({
                    "product_key": pk,
                    "reason": "directional_accuracy_below_minimum",
                    "dir_accuracy": round(dir_accuracy, 3),
                    "min_accuracy": min_accuracy,
                    "scored": scored,
                })
                continue
        else:
            skipped += 1
            details.append({"product_key": pk, "reason": "no_scored_with_actuals", "scored": scored})
            continue

        try:
            report = calibrate_thresholds(
                db,
                product_key=pk,
                days=90,
                buy_range=(0.60, 0.85),
                sell_range=(0.15, 0.40),
                min_change=0.03,
            )
        except Exception:
            logger.warning("autotune calibrate_thresholds failed for %s", pk, exc_info=True)
            skipped += 1
            continue

        if not report.top_results:
            skipped += 1
            details.append({"product_key": pk, "reason": "no_results"})
            continue

        best = report.top_results[0]
        if best is None:
            skipped += 1
            continue

        best_roi = best.get("weighted_roi", 0.0)
        # Check guardrails
        if report.sample_size < min_decisions:
            skipped += 1
            continue

        # Skip if best ROI is not meaningfully positive
        if best_roi < gap_threshold:
            skipped += 1
            details.append({
                "product_key": pk,
                "reason": "roi_below_gap",
                "best_roi": round(best_roi, 3),
            })
            continue

        # Stage the config (best is a dict from grid_search_thresholds)
        row = ForecastThresholdConfig(
            product_key=pk,
            buy_threshold=float(best.get("buy_threshold", 0.70)),
            sell_threshold=float(best.get("sell_threshold", 0.30)),
            buy_min_change=float(best.get("buy_min_change", 0.03)),
            sell_min_change=float(best.get("sell_min_change", -0.03)),
            edge_threshold=float(best.get("edge_threshold", 0.55)),
            source="autotune",
            status="staged",
            org_id=t.org_id,
        )
        db.add(row)
        db.flush()
        staged += 1
        details.append({
            "product_key": pk,
            "reason": "staged",
            "best_roi": round(best_roi, 3),
            "sample_size": report.sample_size,
        })

    return {
        "products_checked": products_checked,
        "staged": staged,
        "skipped": skipped,
        "details": details,
    }
