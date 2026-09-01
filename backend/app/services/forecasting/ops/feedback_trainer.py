"""Feedback-driven adjustment: incorporate scored user corrections into forecasts.

When a user corrects a forecast and the correction is scored (beat=True),
we compute a feedback-driven adjustment that shifts the forecast toward
recent user corrections. The adjustment is:

    adjustment = weighted_mean(user_correction - ai_forecast)

where weights reflect the quality of the correction:

    weight = (1 - user_error / ai_error) * recency_weight

This is applied as a post-processing step after the XGBoost forecast,
rather than modifying the model training (which would require sample_weight
support in the XGBoost model). The adjustment is dampened by recency:

    recency_weight = exp(-days_since_feedback / 30)

so older feedback has less influence.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def compute_feedback_adjustment(
    db,
    target_id: str,
    forecast_values: list[float],
    forecast_date: datetime,
    max_rows: int = 50,
    decay_days: float = 30.0,
) -> float:
    """Compute a feedback-driven adjustment for a forecast.

    Parameters
    ----------
    db : Session
        SQLAlchemy DB session.
    target_id : str
        ForecastTarget.id.
    forecast_values : list[float]
        Current AI forecast values (point forecast).
    forecast_date : datetime
        Date of the forecast (for recency weighting).
    max_rows : int
        Maximum number of feedback rows to consider.
    decay_days : float
        Half-life for recency decay (days).

    Returns
    -------
    float
        Adjustment to add to the forecast (can be positive or negative).
        Returns 0.0 if no applicable feedback.
    """
    from app.models.forecasting import ForecastFeedback

    rows = (
        db.query(ForecastFeedback)
        .filter(
            ForecastFeedback.target_id == target_id,
            ForecastFeedback.beat == True,  # noqa: E712
            ForecastFeedback.status == "scored",
        )
        .order_by(ForecastFeedback.scored_at.desc().nullslast())
        .limit(max_rows)
        .all()
    )

    if not rows:
        return 0.0

    adjustments = []
    weights = []

    for row in rows:
        ai_error = row.ai_error or 0.0
        user_error = row.user_error or 0.0

        # Compute correction magnitude: user_price - ai_price
        ai_price = float(row.ai_price) if row.ai_price else 0.0
        user_price = float(row.user_price) if row.user_price else ai_price
        correction = user_price - ai_price

        # Quality weight: higher when user was much more accurate than AI
        if ai_error > 0 and user_error >= 0:
            quality_weight = max(0.1, 1.0 - user_error / ai_error)
        else:
            quality_weight = 0.5

        # Recency decay: use scored_at if available, else target_date
        ref_date = row.scored_at or row.target_date or forecast_date
        if ref_date is None:
            continue
        days_ago = (forecast_date - ref_date).total_seconds() / 86400.0
        recency_weight = math.exp(-days_ago / decay_days)

        total_weight = quality_weight * recency_weight
        adjustments.append(correction)
        weights.append(total_weight)

    if not adjustments:
        return 0.0

    weights = np.array(weights)
    adjustments = np.array(adjustments)

    # Weighted mean adjustment
    total_weight = weights.sum()
    if total_weight < 1e-10:
        return 0.0

    weighted_adjustment = float(np.average(adjustments, weights=weights))

    # Dampen: don't shift more than 10% of the forecast magnitude
    forecast_mean = float(np.mean(forecast_values)) if forecast_values else 0.0
    max_shift = abs(forecast_mean) * 0.10 if forecast_mean != 0 else 10.0
    dampened = max(-max_shift, min(max_shift, weighted_adjustment))

    logger.info(
        "Feedback adjustment for %s: %.2f (raw=%.2f, n=%d, avg_weight=%.3f)",
        target_id, dampened, weighted_adjustment,
        len(adjustments), float(np.mean(weights)),
    )
    return dampened


def apply_feedback_adjustment(
    forecast_values: list[float],
    adjustment: float,
) -> list[float]:
    """Apply a feedback adjustment to forecast values.

    The adjustment is applied uniformly to all horizon steps but with
    a slight dampening for longer horizons (the model is less certain
    further out, so we apply less feedback correction).

    Parameters
    ----------
    forecast_values : list[float]
        Original forecast values.
    adjustment : float
        Adjustment to apply (from compute_feedback_adjustment).

    Returns
    -------
    list[float]
        Adjusted forecast values.
    """
    if adjustment == 0.0:
        return list(forecast_values)

    adjusted = []
    for i, val in enumerate(forecast_values):
        # Dampen by horizon: step 0 gets full adjustment, step 6 gets 70%
        horizon_dampen = 1.0 - 0.05 * i
        adjusted.append(val + adjustment * horizon_dampen)

    return adjusted
