"""Decision logger — thin DB write wrapper (Phase F2).

Logs each forecast decision to the forecast_decision_logs table.
Enables Wave 1 ROI backtest and threshold calibration (T1.4).
"""
from __future__ import annotations

import logging
from datetime import datetime, date, timezone
from typing import Any, Optional, Sequence

from sqlalchemy.orm import Session

from app.models.forecasting import ForecastDecisionLog

logger = logging.getLogger(__name__)


def log_decision(
    session: Session,
    *,
    product_id: str,
    horizon_day: int,
    as_of_date: date,
    action: str,
    confidence: str = "low",
    rationale: str = "",
    # NOTE (2026-08-05): was incorrectly declared ``int`` here. The
    # underlying column is a UUID String(36) FK to ``forecast_runs.id``;
    # callers pass the run's UUID string, not an integer.
    forecast_run_id: str | None = None,
    predicted_p_rise: float | None = None,
    predicted_change_pct: float | None = None,
    decision_thresholds: dict[str, float] | None = None,
    organization_id: int | None = None,
    # Wave 2 T2.1: actual_price_t at decision time — the critical field
    # that enables get_pending_unrealized() to find logs and close the
    # decision→ROI loop. Captured from the price series tail in the
    # engine's compute pipeline.
    actual_price_t: float | None = None,
) -> ForecastDecisionLog:
    """Create and persist a single decision log row.

    Returns the persisted ForecastDecisionLog instance.
    """
    log = ForecastDecisionLog(
        org_id=organization_id or session.info.get("organization_id"),
        product_id=product_id,
        forecast_run_id=forecast_run_id,
        horizon_day=horizon_day,
        as_of_date=as_of_date,
        predicted_p_rise=predicted_p_rise,
        predicted_change_pct=predicted_change_pct,
        decision_thresholds=decision_thresholds,
        action=action,
        confidence=confidence,
        rationale=rationale,
        actual_price_t=actual_price_t,
    )
    session.add(log)
    session.flush()
    return log


def log_decision_batch(
    session: Session,
    decisions: list[dict[str, Any]],
    *,
    organization_id: int | None = None,
) -> list[ForecastDecisionLog]:
    """Persist multiple decision logs in a single batch.

    Each dict must have: product_id, horizon_day, as_of_date, action.
    Optional: confidence, rationale, forecast_run_id, predicted_p_rise,
              predicted_change_pct, decision_thresholds.
    """
    logs: list[ForecastDecisionLog] = []
    for d in decisions:
        log = log_decision(
            session=session,
            product_id=d["product_id"],
            horizon_day=d["horizon_day"],
            as_of_date=d["as_of_date"],
            action=d["action"],
            confidence=d.get("confidence", "low"),
            rationale=d.get("rationale", ""),
            forecast_run_id=d.get("forecast_run_id"),
            predicted_p_rise=d.get("predicted_p_rise"),
            predicted_change_pct=d.get("predicted_change_pct"),
            decision_thresholds=d.get("decision_thresholds"),
            organization_id=organization_id or session.info.get("organization_id"),
        )
        logs.append(log)
    return logs


def fill_realized_outcomes(
    session: Session,
    # NOTE (2026-08-05): was incorrectly declared ``int`` here. The PK
    # is a UUID String(36) from ``TimestampedBase``; the accuracy_tracker
    # already calls this with ``log_id=log.id`` (a UUID string), so the
    # previous int annotation would have silently failed to fetch the
    # row at runtime.
    log_id: str,
    *,
    actual_price_t: float,
    actual_price_th: float,
    roi_pct: float,
) -> ForecastDecisionLog | None:
    """Fill realized outcome fields for a decision whose window has closed.

    Called by the accuracy_tracker when horizon_day has elapsed.
    """
    log = session.get(ForecastDecisionLog, log_id)
    if log is None:
        logger.warning("Decision log %s not found for realized update", log_id)
        return None
    log.actual_price_t = actual_price_t
    log.actual_price_th = actual_price_th
    log.roi_pct = roi_pct
    log.realized_at = datetime.now(timezone.utc)
    session.flush()
    return log


def get_pending_unrealized(
    session: Session,
    *,
    cutoff_date: date | None = None,
    product_id: str | None = None,
) -> list[ForecastDecisionLog]:
    """Fetch decision logs whose realized window has closed but not yet scored.

    A log is "realizable" when as_of_date + horizon_day <= today AND
    actual_price_t is NOT NULL but roi_pct IS NULL.
    """
    from datetime import date as _date
    from sqlalchemy import and_

    today = cutoff_date or _date.today()

    q = session.query(ForecastDecisionLog).filter(
        ForecastDecisionLog.roi_pct.is_(None),
        ForecastDecisionLog.actual_price_t.isnot(None),
    )
    if product_id:
        q = q.filter(ForecastDecisionLog.product_id == product_id)

    rows = q.all()

    # Filter to logs whose horizon has passed (Python-side — avoids
    # PostgreSQL/SQLite dialect issues with date arithmetic in SQL).
    from datetime import timedelta
    result: list[ForecastDecisionLog] = []
    for r in rows:
        if r.as_of_date is None:
            continue
        # as_of_date may be datetime or date; always coerce to date
        as_date = r.as_of_date.date() if hasattr(r.as_of_date, 'date') else r.as_of_date
        if (as_date + timedelta(days=r.horizon_day)) <= today:
            result.append(r)
    return result
