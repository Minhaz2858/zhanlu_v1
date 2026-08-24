"""Drift auto-response: detect drift (now data-backed) and write an audit row
that the engine will blend toward the naive baseline on the next run."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from app.models.forecasting import ForecastTarget, ForecastWeightAdjustment
from app.services.forecasting.accuracy_tracker import detect_drift

logger = logging.getLogger(__name__)

_DRIFT_AUDIT_WINDOW_DAYS = 7


def get_drift_blend_factor() -> float:
    try:
        from app.config import settings
        f = float(getattr(settings, "FORECAST_DRIFT_BLEND_FACTOR", 0.2))
    except (TypeError, ValueError):
        f = 0.2
    return max(0.0, min(f, 0.5))


def check_drift_and_audit(db, target: ForecastTarget) -> dict:
    """Detect drift for a target; if drifting and no unapplied audit row exists
    in the last 7 days, write one (applied=False). Returns detect_drift dict."""
    result = detect_drift(db, target.product_key)
    if not result.get("is_drifting"):
        return result

    cutoff = datetime.now(timezone.utc) - timedelta(days=_DRIFT_AUDIT_WINDOW_DAYS)
    existing = db.query(ForecastWeightAdjustment).filter(
        ForecastWeightAdjustment.target_id == target.id,
        ForecastWeightAdjustment.triggered_by == "drift",
        ForecastWeightAdjustment.applied == False,  # noqa: E712
        ForecastWeightAdjustment.created_date >= cutoff,
    ).first()
    if existing is not None:
        return result  # already pending; engine will apply it

    audit = ForecastWeightAdjustment(
        target_id=target.id, org_id=target.org_id, app_id=target.app_id,
        triggered_by="drift",
        reason=result.get("reason", "drift detected"),
        old_weights=None, new_weights=None, delta_ratio=None,
        applied=False,
    )
    db.add(audit)
    db.commit()
    logger.warning(
        "[drift-response] %s drifting - audit row written for naive-blend (factor=%.2f)",
        target.product_key, get_drift_blend_factor(),
    )
    return result
