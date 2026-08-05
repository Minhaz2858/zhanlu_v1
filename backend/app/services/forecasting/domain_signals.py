"""EDIA-ported domain signal layers for the forecast engine.

Two pure-Python overlay layers that encode petrochem supply-chain economics
the statistical ensemble (ARIMA + XGBoost + STL) cannot infer from short
price series alone:

1. **Tier-dampened causal-chain elasticities** — naphtha is the root feedstock.
   When naphtha moves X%, downstream products move X × (raw_elasticity ×
   tier_dampening)%. Three tiers model propagation attenuation:
     Tier 1 (naphtha → C5):        ×1.00
     Tier 2 (C5 → downstream):     ×0.85
     Tier 3 (downstream → end-use): ×0.72

2. **Per-product seasonal adjustment rules** — additive % by (product_id, month).
   Captures demand seasonality (winter dip, spring recovery).

Source: EDIA_5.1.2/backend/src/service/forecasting_engine.py:113-179 (verbatim).

Integration: called by ForecastEngine.compute_target() at Step 8.55, between
the intelligence overlay (8.5) and the policy service (8.6). Gated by
FORECAST_DOMAIN_SIGNALS_ENABLED (default False).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier-dampened causal-chain elasticities (EDIA §6.3)
# ---------------------------------------------------------------------------

# product_id: (raw_elasticity, tier_dampening_factor)
# Tier 1 (naphtha → C5): ×1.00   Tier 2 (C5 → downstream): ×0.85
# Tier 3 (downstream → end-use): ×0.72
_RAW_ELASTICITIES: dict[str, tuple[float, float]] = {
    "naphtha": (1.00, 1.00),
    "cracked_c5": (0.82, 1.00),  # Tier 1
    "mixed_c5": (0.82, 1.00),  # Tier 1
    "cracked_c9": (0.65, 0.85),
    "isoprene": (0.88, 0.85),  # Tier 2: 0.88 × 0.85 = 0.748
    "piperylene": (0.85, 0.85),  # Tier 2: 0.85 × 0.85 = 0.7225
    "dcpd": (0.78, 0.85),  # Tier 2: 0.78 × 0.85 = 0.663
    "raffinate_c5": (0.70, 0.85),
    "styrene": (0.55, 0.85),  # Tier 2: 0.55 × 0.85 = 0.4675
    "sis": (0.71, 0.72),  # Tier 3: 0.71 × 0.72 = 0.5112
    "sis_rubber": (0.71, 0.72),  # Tier 3
    "blowing_agent": (0.70, 0.85),
    "butadiene": (0.65, 0.85),
    "sbs": (0.60, 0.72),  # Tier 3
    "c5_resin": (0.62, 0.72),  # Tier 3: 0.62 × 0.72 = 0.4464
    "mixed_c9": (0.65, 0.85),
    "hydrogenated_c9": (0.60, 0.72),
    "trimethylbenzene": (0.60, 0.72),
    "c9_resin": (0.60, 0.72),  # legacy alias
}

# Pre-computed effective coefficients = raw × dampening
_ELASTICITIES: dict[str, float] = {
    k: round(raw * damp, 4) for k, (raw, damp) in _RAW_ELASTICITIES.items()
}


def get_elasticity(product_id: str) -> float | None:
    """Public accessor for the effective chain elasticity of a product."""
    return _ELASTICITIES.get(product_id)


# ---------------------------------------------------------------------------
# Seasonal rules (product_id, month) → pct adjustment (additive)
# ---------------------------------------------------------------------------

_SEASONAL_RULES: dict[tuple[str, int], float] = {
    ("c5_resin", 11): -2.5,
    ("c5_resin", 12): -2.5,
    ("c5_resin", 1): -2.5,
    ("c5_resin", 2): -2.5,
    ("c5_resin", 3): 1.0,
    ("c5_resin", 4): 1.0,
    ("c5_resin", 5): 1.0,
    ("c5_resin", 6): 1.0,
    ("c5_resin", 7): 1.0,
    ("c5_resin", 8): 1.0,
    ("c5_resin", 9): 1.0,
    ("c5_resin", 10): 1.0,
    ("dcpd", 11): -3.0,
    ("dcpd", 12): -3.0,
    ("dcpd", 1): -3.0,
    ("dcpd", 2): -3.0,
    ("dcpd", 3): 1.5,
    ("dcpd", 4): 1.5,
    ("dcpd", 5): 1.5,
    ("dcpd", 6): 1.5,
    ("dcpd", 7): 1.5,
    ("dcpd", 8): 1.5,
    ("isoprene", 2): 0.8,
    ("isoprene", 3): 0.8,
    ("cracked_c5", 1): -1.0,
    ("cracked_c5", 2): -1.0,
    ("cracked_c5", 3): 0.5,
    ("cracked_c5", 4): 0.5,
    ("cracked_c5", 5): 0.5,
    ("piperylene", 2): 1.2,
    ("piperylene", 3): 1.2,
    ("sis", 2): 0.5,
    ("sis", 3): 0.8,
    ("sis", 9): 0.5,
}


# ---------------------------------------------------------------------------
# Overlay functions
# ---------------------------------------------------------------------------

def compute_seasonal_adjustment(product_id: str, month: int) -> float:
    """Return the seasonal % adjustment for a product in a given month.

    Args:
        product_id: The dashboard product_id (e.g. "c5_resin", "naphtha").
        month: Month number 1-12.

    Returns:
        Additive percentage adjustment (e.g. -2.5 means −2.5%).
        Returns 0.0 if no rule exists for this (product_id, month).
    """
    return _SEASONAL_RULES.get((product_id, month), 0.0)


def compute_causal_chain_adjustment(
    product_id: str,
    naphtha_pct_change: float | None,
) -> float:
    """Propagate a naphtha price change through the causal chain.

    Args:
        product_id: The product whose forecast is being adjusted.
        naphtha_pct_change: The recent % change in naphtha price
            (e.g. 10.0 means naphtha rose 10%). None if no signal.

    Returns:
        The propagated % adjustment for this product (e.g. 4.464 means
        +4.464%). Returns 0.0 if naphtha_pct_change is None or the
        product is not in the elasticity table.
    """
    if naphtha_pct_change is None:
        return 0.0
    elasticity = _ELASTICITIES.get(product_id)
    if elasticity is None:
        return 0.0
    return round(naphtha_pct_change * elasticity, 4)


def compute_domain_signal_adjustment(
    product_id: str,
    as_of_date: datetime,
    naphtha_pct_change: float | None = None,
) -> dict[str, Any]:
    """Combined domain-signal overlay: seasonal + causal-chain.

    Called by ForecastEngine.compute_target() at Step 8.55. Returns a dict
    suitable for embedding in the forecast run's explanation.

    Args:
        product_id: The dashboard product_id.
        as_of_date: The forecast anchor date (determines the month for
            seasonal rules).
        naphtha_pct_change: Recent % change in naphtha price, or None.

    Returns:
        dict with keys:
            seasonal_pct: float — additive % from seasonal rules
            causal_pct: float — additive % from causal-chain propagation
            total_pct: float — seasonal_pct + causal_pct
            applied_rules: list[str] — human-readable rule identifiers
                          (e.g. "seasonal:c5_resin:12", "causal:c5_resin:naphtha=+10.0%")
    """
    applied_rules: list[str] = []

    seasonal_pct = compute_seasonal_adjustment(product_id, as_of_date.month)
    if seasonal_pct != 0.0:
        applied_rules.append(f"seasonal:{product_id}:{as_of_date.month}")

    causal_pct = compute_causal_chain_adjustment(product_id, naphtha_pct_change)
    if causal_pct != 0.0 and naphtha_pct_change is not None:
        sign = "+" if naphtha_pct_change >= 0 else ""
        applied_rules.append(
            f"causal:{product_id}:naphtha={sign}{naphtha_pct_change}%"
        )

    total_pct = round(seasonal_pct + causal_pct, 4)

    return {
        "seasonal_pct": seasonal_pct,
        "causal_pct": causal_pct,
        "total_pct": total_pct,
        "applied_rules": applied_rules,
    }


# ---------------------------------------------------------------------------
# Naphtha signal fetcher
# ---------------------------------------------------------------------------

_NAPHTHA_LOOKBACK_DAYS = 14
_NAPHTHA_RECENT_WINDOW = 3  # last 3 days vs preceding 11


def fetch_naphtha_recent_pct_change(
    db: Any,
    org_id: str = "default-org",
    lookback_days: int = _NAPHTHA_LOOKBACK_DAYS,
) -> float | None:
    """Compute the recent % change in naphtha price.

    Reads the naphtha forecast target's source history (last 14 days) and
    compares the mean of the last 3 days against the mean of the preceding
    11 days. This is robust to single-day spikes.

    Args:
        db: SQLAlchemy session (sync).
        org_id: Org ID for the naphtha target.
        lookback_days: Total lookback window (default 14).

    Returns:
        The % change (e.g. 10.0 means naphtha rose 10%), or None if the
        naphtha target or its history is unavailable.
    """
    try:
        from app.models.forecasting import ForecastTarget
        from app.services.forecasting.edia_source import EdiaMysqlDataSource

        target = db.query(ForecastTarget).filter(
            ForecastTarget.product_key == "ecisco.naphtha",
            ForecastTarget.org_id == org_id,
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).first()
        if target is None or not target.datasource:
            return None

        src = EdiaMysqlDataSource()
        df = src.read_history(target.datasource)
        if df is None or len(df) < lookback_days:
            return None

        # Take the last `lookback_days` rows
        recent = df.tail(lookback_days).reset_index(drop=True)
        if len(recent) <= _NAPHTHA_RECENT_WINDOW:
            return None
        recent_mean = recent.tail(_NAPHTHA_RECENT_WINDOW)["y"].mean()
        preceding_mean = recent.head(len(recent) - _NAPHTHA_RECENT_WINDOW)["y"].mean()

        if preceding_mean == 0 or preceding_mean is None or recent_mean is None:
            return None

        pct = round((recent_mean - preceding_mean) / preceding_mean * 100.0, 2)
        logger.info(
            "[domain-signals] naphtha %s: recent_mean=%.2f, preceding_mean=%.2f → %+.2f%%",
            org_id, recent_mean, preceding_mean, pct,
        )
        return pct
    except Exception as exc:
        logger.warning("[domain-signals] naphtha pct change fetch failed: %s", exc)
        return None
