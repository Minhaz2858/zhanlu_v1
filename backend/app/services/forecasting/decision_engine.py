"""Buy/hold/sell decision engine — Phase E Task E2.

Combines:
  - ``p_rise``: probability of price rising (from Phase D conformal probability)
  - ``expected_change_pct``: expected % change (from Phase D)
  - ``directional_acc`` / ``directional_status``: from Phase E1 classifier
  - ``trust_tier``: from `forecast_trust_tier.py` (high/medium/directional/low)

Decision logic:
  - "watch" (low confidence) when no statistical edge OR low trust tier
  - "buy" when p_rise ≥ 0.70 AND expected_change > 3% AND has edge
  - "sell" when p_rise ≤ 0.30 AND expected_change < -3% AND has edge
  - "hold" otherwise

Confidence:
  - "high" only when trust_tier=high AND |p_rise - 0.5| > 0.25
  - "medium" for actionable signals with directional but not high trust
  - "low" for everything else (including all "watch" decisions)
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings

# Decision thresholds — centralized via config.py (Wave 1).
# All defaults match the original hardcoded values → zero regression when flags off.
# Wave 2 T2.4: get_thresholds(product_key) resolves DB → settings → default.
_BUY_THRESHOLD = settings.FORECAST_BUY_THRESHOLD
_SELL_THRESHOLD = settings.FORECAST_SELL_THRESHOLD
_BUY_MIN_CHANGE = settings.FORECAST_BUY_MIN_CHANGE
_SELL_MIN_CHANGE = settings.FORECAST_SELL_MIN_CHANGE
_EDGE_THRESHOLD = settings.FORECAST_EDGE_THRESHOLD
_P_HIGH_MARGIN = settings.FORECAST_P_HIGH_MARGIN


def get_thresholds(
    product_key: str | None = None,
    db: object | None = None,
) -> dict[str, float]:
    """Resolve decision thresholds: DB (active product-specific → active
    global) → env → hardcoded default.

    Used by recommend() to read per-product config at decision time.
    No DB call overhead when no config rows exist (quick query).

    Args:
        product_key: Optional product key for per-product lookup.
        db: Optional pre-existing DB session (for testing).
    """
    _db = db
    _close = False
    try:
        if _db is None:
            from app.database import SessionLocal
            _db = SessionLocal()
            _close = True

        from app.models.forecasting import ForecastThresholdConfig

        try:
            # Active product-specific config
            if product_key:
                row = (
                    _db.query(ForecastThresholdConfig)
                    .filter(
                        ForecastThresholdConfig.product_key == product_key,
                        ForecastThresholdConfig.status == "active",
                    )
                    .order_by(ForecastThresholdConfig.applied_at.desc())
                    .first()
                )
                if row:
                    return {
                        "buy": float(row.buy_threshold),
                        "sell": float(row.sell_threshold),
                        "buy_min_change": float(row.buy_min_change),
                        "sell_min_change": float(row.sell_min_change),
                        "edge": float(row.edge_threshold),
                    }

            # Active global config (product_key IS NULL)
            row = (
                _db.query(ForecastThresholdConfig)
                .filter(
                    ForecastThresholdConfig.product_key.is_(None),
                    ForecastThresholdConfig.status == "active",
                )
                .order_by(ForecastThresholdConfig.applied_at.desc())
                .first()
            )
            if row:
                return {
                    "buy": float(row.buy_threshold),
                    "sell": float(row.sell_threshold),
                    "buy_min_change": float(row.buy_min_change),
                    "sell_min_change": float(row.sell_min_change),
                    "edge": float(row.edge_threshold),
                }
        finally:
            if _close and hasattr(_db, 'close'):
                _db.close()
    except Exception:
        pass

    # Fallback: settings → hardcoded (Wave 1 behavior, zero regression).
    # Read settings at call time (supports hot-reload without restart).
    return {
        "buy": float(getattr(settings, "FORECAST_BUY_THRESHOLD", 0.70)),
        "sell": float(getattr(settings, "FORECAST_SELL_THRESHOLD", 0.30)),
        "buy_min_change": float(getattr(settings, "FORECAST_BUY_MIN_CHANGE", 0.03)),
        "sell_min_change": float(getattr(settings, "FORECAST_SELL_MIN_CHANGE", -0.03)),
        "edge": float(getattr(settings, "FORECAST_EDGE_THRESHOLD", 0.55)),
    }


@dataclass
class Decision:
    action: str          # "buy" | "hold" | "sell" | "watch"
    confidence: str      # "high" | "medium" | "low"
    rationale: str       # human-readable explanation


def recommend(
    p_rise: float,
    expected_change_pct: float,
    directional_acc: float | None,
    directional_status: str | None,
    trust_tier: str,
    product_key: str | None = None,
) -> Decision:
    """Compute buy/hold/sell recommendation.

    Parameters
    ----------
    p_rise : float
        Probability that price[T+h] > price[T]. From Phase D.
    expected_change_pct : float
        Point-forecast expected % change. From Phase D.
    directional_acc : float | None
        Walk-forward directional accuracy. None when classifier unavailable.
    directional_status : str | None
        "edge" / "no_edge" from Phase E1 classifier.
    trust_tier : str
        "high" / "medium" / "directional" / "low".
    product_key : str | None
        Optional product key for per-product threshold lookup (Wave 2 T2.4).
        When None, uses env/default.

    Returns
    -------
    Decision with action, confidence, rationale.
    """
    th = get_thresholds(product_key)
    buy_th = th["buy"]
    sell_th = th["sell"]
    buy_min = th["buy_min_change"]
    sell_min = th["sell_min_change"]
    edge_th = th["edge"]

    tier = (trust_tier or "low").lower()
    has_edge = (
        directional_status == "edge"
        and directional_acc is not None
        and directional_acc >= edge_th
    )

    # Watch zone: no edge, low trust, or any other hedging condition
    if not has_edge or tier == "low":
        msg = (
            "No reliable directional edge; insufficient confidence for a "
            "trade signal."
        )
        if tier == "low":
            msg = "Low trust tier — watch only."
        elif not has_edge:
            msg = "No statistically significant directional edge — watch only."
        return Decision("watch", "low", msg)

    # Determine confidence from trust tier + probability margin
    p_margin = abs(p_rise - 0.5)
    if tier == "high" and p_margin >= _P_HIGH_MARGIN:
        conf = "high"
    elif tier in ("high", "medium"):
        conf = "medium"
    else:  # "directional" tier or anything else actionable
        conf = "medium"

    # Actionable signals
    if p_rise >= buy_th and expected_change_pct > buy_min:
        return Decision(
            "buy", conf,
            f"P(rise)={p_rise:.0%}, expected +{expected_change_pct:.1%}; "
            f"directional accuracy {directional_acc:.0%}.",
        )
    if p_rise <= sell_th and expected_change_pct < sell_min:
        return Decision(
            "sell", conf,
            f"P(rise)={p_rise:.0%}, expected {expected_change_pct:.1%}; "
            f"directional accuracy {directional_acc:.0%}.",
        )
    return Decision(
        "hold", conf,
        f"P(rise)={p_rise:.0%}, expected {expected_change_pct:+.1%}; "
        f"no actionable move.",
    )