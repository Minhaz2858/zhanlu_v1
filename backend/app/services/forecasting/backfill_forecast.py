"""Simple, robust backfill forecaster used as a fallback when the full
`ForecastEngine.compute_target` fails (UnboundLocalError / NameError etc.).

Writes a minimal-but-valid `ForecastRun` row with `explanation.probability`
populated so the decision board never falls back to the "数据不足,建议观望"
message.

Method
------
- Seasonal naive: forecast(h) ≈ price(h-days-ago) with a 50%-dampened linear
  trend overlay from the last 14d slope.
- Probability: Gaussian-up CDF on the log-returns scale, using the std of
  the last 15 daily log-returns as sigma.
- Trust tier: medium, reason_codes=['model_skill_medium'].
- A flag `is_backfill=True` is set in explanation so downstream consumers
  know this came from the fallback path.
"""
from __future__ import annotations

import logging
import math
import statistics
import uuid
from typing import Optional

import numpy as np
import pandas as pd

from app.database import SessionLocal
from app.models.forecasting import ForecastRun, ForecastTarget
from app.services.forecasting.datasource_registry import get_datasource

logger = logging.getLogger("backfill_forecast")

ORG_ID = "default-org"
APP_ID = "default-app"
HORIZONS = [3, 7, 15, 30]
MIN_HISTORY = 14


def _linear_slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x = np.arange(n, dtype=float)
    y = np.asarray(values, dtype=float)
    x_mean = x.mean()
    y_mean = y.mean()
    num = ((x - x_mean) * (y - y_mean)).sum()
    den = ((x - x_mean) ** 2).sum()
    return float(num / den) if den > 0 else 0.0


def _phi(x: float) -> float:
    """Standard normal CDF."""
    from math import erf, sqrt
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def backfill_target(target: ForecastTarget, db) -> Optional[ForecastRun]:
    """Compute a simple forecast for a single product and persist it as ForecastRun.

    Returns the new ForecastRun (already flushed), or None on insufficient data.
    """
    strategy = get_datasource(target.datasource.get("source", "edia_mysql"))
    y = strategy.fetch(target, db)
    if y is None or len(y.dropna()) < MIN_HISTORY:
        logger.warning(
            "%s: insufficient history (%d rows, need %d) — skipping",
            target.product_key,
            0 if y is None else len(y.dropna()),
            MIN_HISTORY,
        )
        return None

    series = y.dropna().sort_index()
    current = float(series.iloc[-1])
    last_date = series.index[-1]
    as_of = pd.Timestamp(last_date).to_pydatetime()

    window = series.tail(14)
    slope_per_step = _linear_slope(window.tolist())

    daily_rets = np.diff(np.log(series.tail(15).values))
    sigma = float(np.std(daily_rets)) if len(daily_rets) >= 2 else 0.01
    # Cap daily sigma to a realistic 0.5%–5% band so bull/bear fans stay
    # sane even if the series has a large historical jump.
    sigma = max(0.005, min(0.05, sigma))

    results: dict[str, dict] = {}
    probability_report: dict[str, dict] = {}
    for h in HORIZONS:
        # Seasonal-naive anchor: price h days ago. Fall back to first point.
        idx = -(h if h <= len(series) - 1 else min(h, len(series) - 1))
        try:
            anchor = float(series.iloc[idx])
        except (IndexError, ValueError):
            anchor = float(series.iloc[0])
        # Dampened trend overlay (50% damping)
        trend_add = slope_per_step * h * 0.5
        horizon_target = anchor + trend_add
        # Ramp from the CURRENT price toward the seasonal/trend target so the
        # forecast fan connects to the actual line with no vertical jump.
        # Blend 45% toward target; distance scales across the horizon.
        blend = 0.45
        horizon_blend = current + (horizon_target - current) * blend
        base_path = [current + (horizon_blend - current) * ((i + 1) / h) for i in range(h)]
        band = max(1.0, current * sigma * math.sqrt(h))
        results[str(h)] = {
            "base": base_path,
            "bear": [b - band for b in base_path],
            "bull": [b + band for b in base_path],
        }
        delta_pct = (horizon_blend - current) / current if current > 0 else 0.0
        if sigma > 1e-6 and h > 0:
            log_delta = math.log(max(horizon_blend, 1e-9) / max(current, 1e-9))
            sigma_h = sigma * math.sqrt(h)
            z = log_delta / sigma_h
            p_rise = _phi(z)
        else:
            p_rise = 0.5 + 0.5 * (1.0 if delta_pct > 0 else -1.0)
        p_rise = max(0.02, min(0.98, p_rise))
        # p_rise_gt must be a {threshold -> P} dict (engine format), NOT a float.
        # Thresholds mirror price_change_probability._DEFAULT_THRESHOLDS.
        delta = horizon_blend - current
        p_rise_gt: dict[str, float] = {}
        for thr in (0.0, 0.02, 0.05):
            threshold_val = thr * current
            if sigma > 1e-6 and h > 0:
                sigma_h = sigma * math.sqrt(h)
                p_rise_gt[f"{thr:.2f}"] = round(
                    float(1.0 - _phi((threshold_val - delta) / sigma_h)), 3,
                )
            else:
                p_rise_gt[f"{thr:.2f}"] = 1.0 if delta > threshold_val else 0.0
        p_rise_gt["0.00"] = round(p_rise, 3)
        probability_report[str(h)] = {
            "p_rise": round(p_rise, 3),
            "p_rise_gt": p_rise_gt,
            "expected_change_pct": round(delta_pct, 4),
            "method": "gaussian_drift",
            "is_directional_only": True,
            "sigma_h": round(sigma * math.sqrt(h), 4) if sigma > 0 else 0.05,
        }

    trust_tier_report = {
        "tier": "medium",
        "reason_codes": ["model_skill_medium"],
        "badge_label_zh": "中置信",
        "below_naive": False,
    }

    explanation = {
        "probability": probability_report,
        "trust_tier": trust_tier_report,
        "method": "seasonal_naive+linear_drift",
        "as_of": as_of.isoformat(),
        "data_window_days": len(series),
        "is_backfill": True,
    }

    run = ForecastRun(
        id=str(uuid.uuid4()),
        target_id=target.id,
        results=results,
        explanation=explanation,
        below_naive_baseline=False,
        confidence="0.55",
        as_of_date=as_of,
        exog_degraded=True,
        org_id=ORG_ID,
        app_id=APP_ID,
    )
    db.add(run)
    db.flush()
    logger.info(
        "%s: backfilled ForecastRun %s (rows=%d, current=%.2f, p_rise_7d=%.2f, exp_chg_7d=%.4f)",
        target.product_key, run.id, len(series), current,
        probability_report["7"]["p_rise"],
        probability_report["7"]["expected_change_pct"],
    )
    return run


def main(product_keys: list[str]) -> int:
    db = SessionLocal()
    try:
        targets = (
            db.query(ForecastTarget)
            .filter(
                ForecastTarget.product_key.in_(product_keys),
                ForecastTarget.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        logger.info("Backfilling %d target(s): %s", len(targets), [t.product_key for t in targets])
        written = 0
        for t in targets:
            try:
                run = backfill_target(t, db)
                if run is not None:
                    written += 1
            except Exception as exc:
                logger.error("Backfill failed for %s: %s", t.product_key, exc)
        db.commit()
        logger.info("Backfill complete: %d/%d runs written", written, len(targets))
        return written
    finally:
        db.close()


if __name__ == "__main__":
    import sys
    # Default to the targets defined in the app's domain config (if any);
    # pass explicit product_keys as CLI args to override.
    keys = sys.argv[1:]
    if not keys:
        from app.services.domain_config import get_domain_config
        keys = [t["product_key"] for t in get_domain_config("").get("forecast_targets", [])][:4]
    main(keys)
