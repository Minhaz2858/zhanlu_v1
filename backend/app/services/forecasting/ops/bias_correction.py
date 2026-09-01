"""Validated bias-correction learning layer.

An author's judgement only shifts a forecast after it has demonstrably beaten
the AI against reality (trust gate). The delta is recency-weighted, clamped to
+/-10%, and applied to the published price only — never the decision call.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from app.models.forecasting import ForecastFeedback, ForecastWeightAdjustment
from app.services.forecasting.ops.feedback_service import author_track_record

logger = logging.getLogger(__name__)

MIN_SCORED = 3
MIN_BEAT_RATE = 0.5
BIAS_CAP = 0.10
RECENCY_HALFLIFE_DAYS = 30


def trust_gate_met(db, author_id: str, product_id: str) -> bool:
    tr = author_track_record(db, author_id, product_id)
    return tr["scored"] >= MIN_SCORED and tr["beat_rate"] > MIN_BEAT_RATE


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def compute_bias_delta(db, product_id: str, author_id: str) -> dict:
    """Recency-weighted mean signed (user_price - ai_price)/ai_price, clamped to +/-BIAS_CAP."""
    rows = db.query(ForecastFeedback).filter(
        ForecastFeedback.product_id == product_id,
        ForecastFeedback.author_id == author_id,
        ForecastFeedback.status == "scored",
        ForecastFeedback.is_deleted == False,  # noqa: E712
    ).all()
    now = datetime.now(timezone.utc)
    deltas, weights = [], []
    for r in rows:
        if r.ai_price <= 0:
            continue
        d = (r.user_price - r.ai_price) / r.ai_price
        ts = r.scored_at or r.created_date
        if ts is None:
            age_days = 0.0
        else:
            age_days = (now - _to_naive_utc(ts)).total_seconds() / 86400.0
        w = 0.5 ** (max(0.0, age_days) / RECENCY_HALFLIFE_DAYS)
        deltas.append(d); weights.append(w)
    if not deltas:
        return {"delta_ratio": 0.0, "clamped": False, "n": 0}
    wsum = sum(weights)
    raw = sum(d * w for d, w in zip(deltas, weights)) / wsum if wsum else 0.0
    clamped = abs(raw) > BIAS_CAP
    delta = max(-BIAS_CAP, min(BIAS_CAP, raw))
    return {"delta_ratio": delta, "clamped": clamped, "n": len(deltas), "raw": raw}


def apply_bias_correction(db, target, published_series: pd.Series, author_id: str | None = None):
    """Returns (adjusted_series, explanation_dict_or_None).

    If no trusted author (or author_id None) -> returns the series unchanged with None.
    """
    if author_id is None:
        return published_series, None
    if not trust_gate_met(db, author_id, target.product_key):
        return published_series, None

    delta = compute_bias_delta(db, target.product_key, author_id)
    if delta["delta_ratio"] == 0.0:
        return published_series, None

    factor = 1.0 + delta["delta_ratio"]
    adjusted = published_series * factor

    audit = ForecastWeightAdjustment(
        target_id=target.id, org_id=target.org_id, app_id=target.app_id,
        triggered_by="bias_correction",
        reason=f"trusted author {author_id} delta={delta['delta_ratio']:.4f} (n={delta['n']})",
        old_weights=None, new_weights=None,
        delta_ratio=delta["delta_ratio"], applied=True, applied_at=datetime.now(timezone.utc),
    )
    db.add(audit); db.commit()

    expl = {
        "author_id": author_id,
        "delta_ratio": delta["delta_ratio"],
        "n_overrides": delta["n"],
        "clamped": delta["clamped"],
    }
    logger.info("[bias-correction] %s applied delta=%.4f (n=%d)",
                target.product_key, delta["delta_ratio"], delta["n"])
    return adjusted, expl


def resolve_trusted_author(db, product_key: str) -> str | None:
    """Most recent scored author for the product who passes the trust gate."""
    rows = db.query(ForecastFeedback).filter(
        ForecastFeedback.product_id == product_key,
        ForecastFeedback.status == "scored",
        ForecastFeedback.is_deleted == False,  # noqa: E712
    ).order_by(ForecastFeedback.scored_at.desc()).limit(50).all()
    seen: list[str] = []
    for r in rows:
        if r.author_id not in seen:
            seen.append(r.author_id)
    for aid in seen:
        if trust_gate_met(db, aid, product_key):
            return aid
    return None
