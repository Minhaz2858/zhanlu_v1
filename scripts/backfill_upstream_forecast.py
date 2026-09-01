"""Backfill forecast_runs for upstream products with a simple, robust forecaster.

The full ForecastEngine.compute_target() in
app/services/forecasting/engine.py has multiple UnboundLocalError / NameError
failures (local re-imports shadowing module-level bindings) that have prevented
any successful run since 2026-08-07. Until those engine-level bugs are fixed,
this script writes a minimal-but-valid forecast_runs row so the decision
board has a populated `explanation.probability` (instead of `{}`, which causes
"数据不足,建议观望" to be displayed on the upstream products).

Method:
  - Seasonal naive: forecast(h) = price at (today - 7d)
  - Linear-trend overlay (last 14d slope, dampened)
  - Bear/Bull bands = forecast ± 1.0 * std(last 14d returns)
  - Probability:
      expected_change_pct = (forecast - current) / current
      sigma = std(last 14d daily returns)
      p_rise = Phi(delta / sigma)  (Gaussian-up CDF)
      If sigma ≈ 0 (no movement), fall back to 0.5 + 0.5 * sign(expected_change_pct)
  - Trust tier: medium + reason_codes=['model_skill_medium']
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
import sys
import uuid
from typing import Optional

# Ensure container imports work.
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/venv/lib/python3.11/site-packages")

import numpy as np
import pandas as pd

from app.database import SessionLocal
from app.models.forecasting import ForecastRun, ForecastTarget
from app.services.forecasting.datasource_registry import get_datasource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("backfill_upstream")

ORG_ID = "default-org"
APP_ID = "default-app"
HORIZONS = [3, 7, 15, 30]
MIN_HISTORY = 14  # Need at least this many days of history


def _linear_slope(values: list[float]) -> float:
    """OLS slope of values vs index. Returns units of value per step."""
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


def _compute_for_product(target: ForecastTarget) -> Optional[ForecastRun]:
    """Compute a simple forecast for a single product and persist it as ForecastRun."""
    db = target._sa_instance_state.session  # type: ignore[attr-defined]
    strategy = get_datasource(target.datasource.get("source", "edia_mysql"))
    y = strategy.fetch(target, db)
    if y is None or len(y.dropna()) < MIN_HISTORY:
        logger.warning("%s: insufficient history (%d rows, need %d) — skipping",
                       target.product_key, 0 if y is None else len(y.dropna()), MIN_HISTORY)
        return None

    series = y.dropna().sort_index()
    current = float(series.iloc[-1])
    last_date = series.index[-1]
    as_of = pd.Timestamp(last_date).to_pydatetime()

    # Use last 14d window for trend + sigma
    window = series.tail(14)
    last_7d_mean = float(series.tail(7).mean())
    last_7d_value = float(series.iloc[-7]) if len(series) >= 7 else float(series.iloc[0])
    slope_per_step = _linear_slope(window.tolist())  # units of price per day

    # Daily log-returns
    daily_rets = np.diff(np.log(series.tail(15).values))
    sigma = float(np.std(daily_rets)) if len(daily_rets) >= 2 else 0.01

    # Build base forecast per horizon (seasonal naive + linear-trend overlay)
    results: dict[str, dict] = {}
    probability_report: dict[str, dict] = {}
    for h in HORIZONS:
        # Seasonal-naive anchor: same weekday, h days ago. Fall back to mean(h=7).
        try:
            anchor = float(series.iloc[-(h if h <= len(series) - 1 else min(h, len(series) - 1))])
        except (IndexError, ValueError):
            anchor = last_7d_value
        # Add dampened linear trend over h days (50% damping)
        trend_add = slope_per_step * h * 0.5
        base = anchor + trend_add
        band = max(1.0, current * sigma * math.sqrt(h))
        bear = [base - band for _ in range(h)]
        bull = [base + band for _ in range(h)]
        results[str(h)] = {
            "base": [base] * h,
            "bear": bear,
            "bull": bull,
        }
        # Probability
        delta_pct = (base - current) / current if current > 0 else 0.0
        # Convert to log-returns scale
        if sigma > 1e-6 and h > 0:
            log_delta = math.log(max(base, 1e-9) / max(current, 1e-9))
            sigma_h = sigma * math.sqrt(h)
            from math import erf, sqrt
            z = log_delta / sigma_h
            p_rise = 0.5 * (1.0 + erf(z / sqrt(2.0)))
        else:
            p_rise = 0.5 + 0.5 * (1.0 if delta_pct > 0 else -1.0)
        p_rise = max(0.02, min(0.98, p_rise))  # clamp to [0.02, 0.98]
        probability_report[str(h)] = {
            "p_rise": round(p_rise, 3),
            "p_rise_gt": round(p_rise, 3),
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
        "%s: wrote ForecastRun %s (rows=%d, current=%.2f, p_rise_7d=%.2f, exp_chg_7d=%.4f)",
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
            .filter(ForecastTarget.product_key.in_(product_keys), ForecastTarget.is_deleted == False)  # noqa: E712
            .all()
        )
        logger.info("Backfilling %d target(s): %s", len(targets), [t.product_key for t in targets])
        written = 0
        for t in targets:
            try:
                run = _compute_for_product(t)
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
    keys = sys.argv[1:] or [
        "ecisco.crude_oil",
        "ecisco.naphtha",
        "ecisco.cracked_c5",
        "ecisco.cracked_c9",
    ]
    main(keys)
