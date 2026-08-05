"""Threshold calibrator — grid-search decision thresholds from scored logs (Wave 1).

Wraps ``features/decision_roi.grid_search_thresholds`` with safety guardrails:
- Minimum sample size ≥ 10 scored decisions
- Buy/sell threshold gap ≥ 0.15
- Best ROI must be positive
- Returns advisory ``CalibrationReport``; does NOT auto-apply.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.services.forecasting.features.decision_roi import grid_search_thresholds

logger = logging.getLogger(__name__)


@dataclass
class CalibrationReport:
    """Result of threshold calibration against historical decision logs."""
    product_key: Optional[str]
    sample_size: int
    date_range: tuple[str, str]  # (min_date, max_date) as ISO strings
    top_results: list[dict] = field(default_factory=list)
    recommendation: Optional[dict] = None
    safety_checks: dict = field(default_factory=lambda: {
        "min_samples": False,
        "gap_ok": False,
        "roi_positive": False,
    })
    warnings: list[str] = field(default_factory=list)


def calibrate_thresholds(
    db: Session,
    *,
    product_key: Optional[str] = None,
    days: int = 90,
    buy_range: Optional[tuple[float, float]] = None,
    sell_range: Optional[tuple[float, float]] = None,
    min_change: float = 0.03,
) -> CalibrationReport:
    """Query scored decision logs and grid-search optimal thresholds.

    Parameters
    ----------
    db : Session
        SQLAlchemy DB session.
    product_key : str, optional
        Filter to a specific product forecast target key.
    days : int
        Lookback window in days (default 90).
    buy_range : tuple[float, float], optional
        (low, high) grid for buy threshold. Default (0.55, 0.85).
    sell_range : tuple[float, float], optional
        (low, high) grid for sell threshold. Default (0.15, 0.45).
    min_change : float
        Minimum expected % change to consider actionable (default 0.03).

    Returns
    -------
    CalibrationReport with top-3 results, recommendation, and safety checks.
    """
    from app.models.forecasting import ForecastDecisionLog

    warnings: list[str] = []
    cutoff = date.today() - timedelta(days=days)

    query = db.query(ForecastDecisionLog).filter(
        ForecastDecisionLog.roi_pct.isnot(None),
        ForecastDecisionLog.as_of_date >= cutoff,
    )
    if product_key:
        query = query.filter(ForecastDecisionLog.product_id == product_key)

    logs = query.all()

    sample_size = len(logs)
    report = CalibrationReport(
        product_key=product_key,
        sample_size=sample_size,
        date_range=(
            cutoff.isoformat(),
            date.today().isoformat(),
        ),
    )

    # Safety check 1: minimum sample size
    if sample_size < 10:
        report.warnings.append(
            f"Insufficient data: {sample_size} scored logs (need ≥ 10). "
            "Calibration not possible."
        )
        return report
    report.safety_checks["min_samples"] = True

    # Convert logs to dicts consumed by grid_search_thresholds
    log_dicts: list[dict] = []
    for log in logs:
        log_dicts.append({
            "action": log.action,
            "realized_price_t": log.actual_price_t,
            "realized_price_th": log.actual_price_th,
            "p_rise": log.predicted_p_rise or 0.0,
            "expected_change_pct": log.predicted_change_pct or 0.0,
        })

    # Run grid search
    results = grid_search_thresholds(
        log_dicts,
        buy_range=buy_range,
        sell_range=sell_range,
        min_change=min_change,
    )

    if not results:
        report.warnings.append("Grid search returned no results.")
        return report

    # Sort by ROI descending, take top 3
    results.sort(key=lambda r: r.get("roi_pct", -9999), reverse=True)
    report.top_results = results[:3]

    best = results[0]
    buy_t = best.get("buy_threshold", 0.70)
    sell_t = best.get("sell_threshold", 0.30)
    best_roi = best.get("roi_pct", 0)

    # Safety check 2: buy/sell gap ≥ 0.15
    gap = buy_t - sell_t
    report.safety_checks["gap_ok"] = gap >= 0.15
    if gap < 0.15:
        report.warnings.append(
            f"Best buy/sell gap ({buy_t:.2f} - {sell_t:.2f} = {gap:.2f}) < 0.15. "
            "Too narrow — may cause excessive trading."
        )

    # Safety check 3: positive ROI
    report.safety_checks["roi_positive"] = best_roi > 0
    if best_roi <= 0:
        report.warnings.append(
            f"Best ROI ({best_roi:.2f}%) is not positive. No actionable calibration found."
        )
        return report

    # All checks passed → recommendation is the best result
    if all(report.safety_checks.values()):
        report.recommendation = {
            "buy_threshold": buy_t,
            "sell_threshold": sell_t,
            "min_change": min_change,
            "roi_pct": best_roi,
            "sample_size": sample_size,
            "grid_position": best.get("grid_position"),
            "env_override": {
                "FORECAST_BUY_THRESHOLD": str(buy_t),
                "FORECAST_SELL_THRESHOLD": str(sell_t),
                "FORECAST_BUY_MIN_CHANGE": str(min_change),
                "FORECAST_SELL_MIN_CHANGE": str(-min_change),
            },
        }

    return report
