"""T2.2 Event-Impact Calibration — populate ForecastEventImpact from real data.

For each closed IntelligenceEvent (whose impact window has passed), compute
the real price/volume impact and write a ForecastEventImpact row for later
consumption by forecast tooling. Matching to products is done generically
against known ForecastTarget product_keys (no hardcoded commodity aliases).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Baseline window before event date (days)
DEFAULT_BASELINE_DAYS = 14


def _compute_event_impact(
    *,
    price_history: dict[str, float],
    event_date: date,
    window_days: int = 7,
    baseline_days: int = DEFAULT_BASELINE_DAYS,
) -> dict[str, Any]:
    """Compute price impact from a daily price-history dict.

    Args:
        price_history: {date_iso_str: price_float, ...}
        event_date: Date the event occurred
        window_days: Post-event observation window
        baseline_days: Pre-event baseline window

    Returns:
        {price_impact_pct, volume_impact_pct, duration_days}
        price_impact_pct may be None when data is insufficient.
    """
    if not price_history or len(price_history) < baseline_days + window_days:
        return {
            "price_impact_pct": None,
            "volume_impact_pct": None,
            "duration_days": None,
        }

    # --- baseline (pre-event) ---
    baseline_prices: list[float] = []
    for i in range(baseline_days, 0, -1):
        d = (event_date - timedelta(days=i)).isoformat()
        p = price_history.get(d)
        if p is not None:
            baseline_prices.append(p)

    if not baseline_prices:
        return {
            "price_impact_pct": None,
            "volume_impact_pct": None,
            "duration_days": None,
        }

    baseline_mean = sum(baseline_prices) / len(baseline_prices)
    if baseline_mean <= 0:
        return {
            "price_impact_pct": None,
            "volume_impact_pct": None,
            "duration_days": None,
        }

    # --- post-event window ---
    post_prices: list[float] = []
    for i in range(window_days):
        d = (event_date + timedelta(days=i)).isoformat()
        p = price_history.get(d)
        if p is not None:
            post_prices.append(p)

    if not post_prices:
        return {
            "price_impact_pct": None,
            "volume_impact_pct": None,
            "duration_days": None,
        }

    post_mean = sum(post_prices) / len(post_prices)
    price_impact_pct = round((post_mean / baseline_mean - 1.0) * 100.0, 3)

    return {
        "price_impact_pct": price_impact_pct,
        "volume_impact_pct": None,
        "duration_days": len(post_prices),
    }


def _get_closed_events(
    db: Session, window_days: int = 7
) -> list[dict[str, Any]]:
    """Fetch IntelligenceEvents whose impact window has closed.

    Matches events to known forecast targets generically (product_key
    substring match against headline text) and returns
    (event, product_key) pairs ready for impact computation.
    """
    from app.models.intelligence import IntelligenceEvent
    from app.models.forecasting import ForecastTarget

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=window_days)
    rows = (
        db.query(IntelligenceEvent)
        .filter(
            IntelligenceEvent.review_status.in_(["approved", "auto_approved"]),
            IntelligenceEvent.created_date <= cutoff,
        )
        .order_by(IntelligenceEvent.created_date.desc())
        .all()
    )

    target_keys = [
        t[0]
        for t in db.query(ForecastTarget.product_key).all()
        if t[0]
    ]

    result: list[dict[str, Any]] = []
    for row in rows:
        ev_date = (
            row.created_date.date()
            if isinstance(row.created_date, datetime)
            else row.created_date
        )
        headline = (getattr(row, "headline", "") or "").lower()
        # Match to each known forecast target
        for pk in target_keys:
            pk_lower = pk.lower()
            if pk_lower not in headline:
                continue
            result.append({
                "id": getattr(row, "event_id", row.id),
                "event_type": getattr(row, "event_type", "unknown"),
                "event_date": ev_date,
                "headline": getattr(row, "headline", ""),
                "product_key": pk,
                "direction": getattr(row, "direction", "neutral") or "neutral",
                "magnitude_estimate": getattr(row, "magnitude_estimate", "minor") or "minor",
            })
    return result


def _get_price_history_for_event(
    db: Session, product_key: str
) -> dict[str, float]:
    """Fetch price history for a product as {date_str: price_float}.

    Reads through the forecasting-native external data source (the market
    dashboard price reader was removed with that feature).
    """
    try:
        from app.services.forecasting.analyst.service import _read_history_rows

        rows = _read_history_rows(product_key)
        out: dict[str, float] = {}
        for date_str, price in rows:
            out[str(date_str)[:10]] = float(price)
        return out
    except Exception:
        logger.warning(
            "_get_price_history_for_event: failed for %s", product_key, exc_info=True
        )
        return {}


def run_event_calibration(
    db: Session,
    lookback_days: int = 180,
    window_days: int = 7,
) -> dict[str, Any]:
    """Main entry point: calibrate closed events → ForecastEventImpact rows.

    Idempotent: skips (product_id, event_type, event_date) combos that
    already exist in ForecastEventImpact.

    Returns: {events_processed, impacts_written, errors}
    """
    from app.models.forecasting import ForecastEventImpact

    events = _get_closed_events(db, window_days=window_days)
    errors: list[str] = []
    impacts_written = 0

    for ev in events:
        pk = ev["product_key"]
        ev_type = ev["event_type"]
        ev_date = ev["event_date"]

        # Idempotency check: skip if already calibrated.
        # Use func.date() for cross-DB date comparison (SQLite stores
        # DateTime columns with full ISO timestamps).
        from sqlalchemy import func
        existing = (
            db.query(ForecastEventImpact)
            .filter(
                ForecastEventImpact.product_id == pk,
                ForecastEventImpact.event_type == ev_type,
                func.date(ForecastEventImpact.event_date) == ev_date,
            )
            .first()
        )
        if existing is not None:
            continue

        try:
            prices = _get_price_history_for_event(db, pk)
            impact = _compute_event_impact(
                price_history=prices,
                event_date=ev_date if isinstance(ev_date, date) else ev_date,
                window_days=window_days,
            )

            row = ForecastEventImpact(
                product_id=pk,
                event_type=ev_type,
                event_date=ev_date,
                event_label=ev.get("headline")[:200] if ev.get("headline") else None,
                price_impact_pct=impact["price_impact_pct"],
                volume_impact_pct=impact["volume_impact_pct"],
                duration_days=impact["duration_days"],
                source="event_calibration",
                confidence_score=0.6,  # moderate default; will improve with volume
                org_id=db.info.get("organization_id", "default-org"),
            )
            db.add(row)
            db.flush()
            impacts_written += 1
        except Exception:
            msg = f"event_calibration error for {pk}/{ev_type}/{ev_date}"
            errors.append(msg)
            logger.warning(msg, exc_info=True)

    db.flush()
    return {
        "events_processed": len(events),
        "impacts_written": impacts_written,
        "errors": errors,
    }
