"""P2.13: Isotonic p_rise calibration layer.

Calibrates predicted_p_rise from ForecastDecisionLog against realized
direction using sklearn.isotonic.IsotonicRegression.

Flag-gated via FORECAST_P_RISE_CALIBRATION_ENABLED (default: false).
Runs as a nightly step after eval + decision-scoring.

When insufficient samples exist (< min_samples), falls back to P0.3
empirical p_rise (residual CDF).
"""
import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Type alias for calibration result dict
CalibratedPRiseResult = dict[str, Any]

_MIN_SAMPLES_DEFAULT = 30
_N_RELIABILITY_BUCKETS = 10


def fit_product_calibration(
    rows: list[dict],
    min_samples: int = _MIN_SAMPLES_DEFAULT,
) -> CalibratedPRiseResult | None:
    """Fit isotonic calibration for one product from scored decisions.

    Parameters
    ----------
    rows : list[dict]
        Each dict must have ``predicted_p_rise`` (float) and ``actual_rise`` (bool).
    min_samples : int
        Minimum scored decisions required for a valid calibration curve.

    Returns
    -------
    dict or None
        ``{'curve': {'x': [...], 'y': [...]}, 'reliability': [...], 'n': int}``
        or None when below min_samples.
    """
    if len(rows) < min_samples:
        return None

    try:
        from sklearn.isotonic import IsotonicRegression
    except ImportError:
        logger.warning("scikit-learn not available — p_rise calibration skipped")
        return None

    predicted = np.array([r["predicted_p_rise"] for r in rows], dtype=float)
    actual = np.array([float(r["actual_rise"]) for r in rows], dtype=float)

    # Filter out NaN/inf
    mask = np.isfinite(predicted) & np.isfinite(actual)
    predicted = predicted[mask]
    actual = actual[mask]

    if len(predicted) < min_samples:
        return None

    # Sort by predicted (isotonic requires monotone input)
    order = np.argsort(predicted)
    predicted_sorted = predicted[order]
    actual_sorted = actual[order]

    ir = IsotonicRegression(out_of_bounds="clip")
    calibrated = ir.fit_transform(predicted_sorted, actual_sorted)

    # Build curve (the isotonic step function)
    curve_x = predicted_sorted.tolist()
    curve_y = calibrated.tolist()

    # Build reliability buckets
    reliability = _compute_reliability(predicted, actual)

    return {
        "curve": {"x": curve_x, "y": curve_y},
        "reliability": reliability,
        "n": len(predicted),
    }


def apply_calibration(
    predicted_p_rise: float,
    calibration: CalibratedPRiseResult,
) -> float:
    """Apply isotonic calibration to a single predicted_p_rise value.

    Uses nearest-neighbor interpolation on the calibration curve.
    Values outside the training range are clamped to [0, 1].
    """
    if not calibration or "curve" not in calibration:
        return max(0.0, min(1.0, predicted_p_rise))

    x = np.array(calibration["curve"]["x"])
    y = np.array(calibration["curve"]["y"])

    if len(x) == 0:
        return max(0.0, min(1.0, predicted_p_rise))

    # Nearest-neighbor interpolation
    idx = np.searchsorted(x, predicted_p_rise, side="right") - 1
    idx = max(0, min(len(y) - 1, idx))
    result = float(y[idx])

    # Clamp to [0, 1]
    return max(0.0, min(1.0, result))


def _compute_reliability(
    predicted: np.ndarray,
    actual: np.ndarray,
    n_buckets: int = _N_RELIABILITY_BUCKETS,
) -> list[dict]:
    """Compute reliability diagram buckets for diagnostic display."""
    edges = np.linspace(0, 1, n_buckets + 1)
    buckets = []
    for i in range(n_buckets):
        lo, hi = edges[i], edges[i + 1]
        mask = (predicted >= lo) & (predicted < hi)
        count = int(mask.sum())
        if count > 0:
            pred_mean = float(predicted[mask].mean())
            obs_rate = float(actual[mask].mean())
        else:
            pred_mean = float((lo + hi) / 2)
            obs_rate = 0.0
        buckets.append({
            "bin_center": float((lo + hi) / 2),
            "predicted_mean": round(pred_mean, 4),
            "observed_rate": round(obs_rate, 4),
            "count": count,
        })
    return buckets


# ---------------------------------------------------------------------------
# Nightly-loop integration: weekly recalibration from realized decisions
# ---------------------------------------------------------------------------

def run_weekly_p_rise_calibration(db, _today=None) -> dict:
    """Weekly p_rise recalibration from realized decision data.

    For each active ForecastTarget, collect recent ForecastDecisionLog rows
    where actual outcomes are available.  Fit isotonic calibration on the
    (predicted_p_rise, actual_rise_indicator) pairs and persist the calibration
    curve to target.model_config["p_rise_calibration"].

    Only runs on Mondays (7-day cycle).  Requires >= 20 realized decisions
    per target for a meaningful fit.

    Args:
        db: SQLAlchemy session.
        _today: Override for datetime.date.today() (for testing).

    Returns {calibrated, skipped, error}.
    """
    import datetime
    from app.models.forecasting import ForecastTarget, ForecastDecisionLog

    # Only run on Mondays
    today = _today or datetime.date.today()
    if today.weekday() != 0:  # 0 = Monday
        logger.info("[p_rise_cal] Not Monday — skipping (today=%s)", today)
        return {"calibrated": 0, "skipped": True, "reason": "not_monday"}

    targets = (
        db.query(ForecastTarget)
        .filter(
            ForecastTarget.org_id == "default-org",
            ForecastTarget.status == "active",
            ForecastTarget.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    calibrated = 0
    skipped = 0
    errors = []

    for target in targets:
        try:
            product_id = target.product_key or target.name
            # Collect realized decisions for this product
            decisions = (
                db.query(ForecastDecisionLog)
                .filter(
                    ForecastDecisionLog.product_id == product_id,
                    ForecastDecisionLog.predicted_p_rise.isnot(None),
                    ForecastDecisionLog.actual_price_t.isnot(None),
                    ForecastDecisionLog.actual_price_th.isnot(None),
                )
                .order_by(ForecastDecisionLog.as_of_date.desc())
                .limit(200)
                .all()
            )

            if len(decisions) < 20:
                skipped += 1
                logger.debug(
                    "[p_rise_cal] %s: only %d decisions (<20), skipping",
                    product_id, len(decisions),
                )
                continue

            # Build (predicted_p_rise, actual_rise) pairs
            rows = []
            for d in decisions:
                actual_rise = bool(d.actual_price_th > d.actual_price_t)
                rows.append({"predicted_p_rise": float(d.predicted_p_rise), "actual_rise": actual_rise})

            result = fit_product_calibration(rows, min_samples=20)

            if result is None:
                skipped += 1
                continue

            # Persist to model_config
            if target.model_config is None:
                target.model_config = {}
            target.model_config["p_rise_calibration"] = {
                "curve": result["curve"],
                "reliability": result["reliability"],
                "n_samples": result["n"],
                "fitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            db.add(target)
            calibrated += 1

        except Exception as exc:
            errors.append(str(exc))
            logger.warning("[p_rise_cal] failed for %s: %s", target.name, exc)

    if calibrated > 0:
        db.commit()

    logger.info(
        "[p_rise_cal] Done: %d calibrated, %d skipped, %d errors",
        calibrated, skipped, len(errors),
    )
    return {
        "calibrated": calibrated,
        "skipped": skipped,
        "errors": errors[:10],
    }
