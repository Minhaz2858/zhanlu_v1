"""Human-in-the-Loop feedback capture + author track-record.

A recorded correction is an ASSERTION, not an action — it never moves the
forecast until the trust gate (bias_correction.py) is earned.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from app.models.forecasting import ForecastTarget, ForecastFeedback

logger = logging.getLogger(__name__)


def record_feedback(
    db,
    product_id: str,
    ai_price: float,
    user_price: float,
    reason: Optional[str],
    author_id: str,
    author_name: Optional[str],
    target_date: Optional[datetime],
) -> ForecastFeedback:
    if user_price <= 0:
        raise ValueError("user_price must be positive")
    if ai_price <= 0:
        raise ValueError("ai_price must be positive")
    target = db.query(ForecastTarget).filter(
        ForecastTarget.product_key == product_id,
        ForecastTarget.is_deleted == False,  # noqa: E712
    ).first()
    if target is None:
        raise ValueError(f"no forecast target for product_id={product_id!r}")
    fb = ForecastFeedback(
        target_id=target.id, org_id=target.org_id, app_id=target.app_id,
        product_id=product_id, ai_price=float(ai_price), user_price=float(user_price),
        reason=reason, author_id=author_id, author_name=author_name,
        target_date=target_date, status="pending",
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    logger.info("[hitl] feedback recorded: %s ai=%s user=%s by=%s",
                product_id, ai_price, user_price, author_id)
    return fb


def list_feedback(db, product_id: str) -> list[dict]:
    rows = db.query(ForecastFeedback).filter(
        ForecastFeedback.product_id == product_id,
        ForecastFeedback.is_deleted == False,  # noqa: E712
    ).order_by(ForecastFeedback.created_date.desc()).limit(100).all()
    return [_fb_dict(r) for r in rows]


def _fb_dict(r: ForecastFeedback) -> dict:
    return {
        "id": r.id, "ai_price": r.ai_price, "user_price": r.user_price,
        "reason": r.reason, "author_name": r.author_name,
        "target_date": r.target_date.isoformat() if r.target_date else None,
        "status": r.status, "beat": r.beat,
        "ai_error": r.ai_error, "user_error": r.user_error,
        "created_date": r.created_date.isoformat() if r.created_date else None,
    }


def author_track_record(db, author_id: str, product_id: str) -> dict:
    """Aggregate an author's SCORED overrides for a product — feeds the trust gate."""
    rows = db.query(ForecastFeedback).filter(
        ForecastFeedback.author_id == author_id,
        ForecastFeedback.product_id == product_id,
        ForecastFeedback.status == "scored",
        ForecastFeedback.is_deleted == False,  # noqa: E712
    ).all()
    scored = len(rows)
    beat = sum(1 for r in rows if r.beat)
    return {
        "scored": scored,
        "beat": beat,
        "beat_rate": (beat / scored) if scored else 0.0,
    }
