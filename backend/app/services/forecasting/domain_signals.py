"""Domain-signal overlay layers for the forecast engine (config-driven).

Two optional overlay layers that encode supply-chain economics the
statistical ensemble (ARIMA + XGBoost + STL) cannot infer from short
price series alone:

1. **Tier-dampened causal-chain elasticities** — the root feedstock drives
   downstream products: when the root moves X%, downstream products move
   X × (raw_elasticity × tier_dampening)%. Three tiers model propagation
   attenuation (Tier 1 → Tier 3).

2. **Per-product seasonal adjustment rules** — additive % by (product_id,
   month). Captures demand seasonality (winter dip, spring recovery).

The elasticities and seasonal rules are DATA loaded from the app's domain
config (``domain_signals`` block). Apps WITHOUT a config get an empty overlay
(no-op) — the statistical ensemble runs unmodified.

Integration: called by ForecastEngine.compute_target() at Step 8.55, between
the intelligence overlay (8.5) and the policy service (8.6). Gated by
FORECAST_DOMAIN_SIGNALS_ENABLED (default False).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.services.domain_config import get_domain_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config-driven data (HOW, not WHAT — WHAT lives in the app's domain config)
# ---------------------------------------------------------------------------

_DOMAIN_SIGNALS_CFG: dict[str, Any] = get_domain_config(
    ""
).get("domain_signals", {})

# product_id: (raw_elasticity, tier_dampening_factor)
_RAW_ELASTICITIES: dict[str, tuple[float, float]] = {
    k: (v[0], v[1]) for k, v in _DOMAIN_SIGNALS_CFG.get("elasticities", {}).items()
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
# JSON keys use "product_id|month" (e.g. "<product>|11")
# ---------------------------------------------------------------------------

_SEASONAL_RULES: dict[tuple[str, int], float] = {
    (k.split("|")[0], int(k.split("|")[1])): v
    for k, v in _DOMAIN_SIGNALS_CFG.get("seasonal_rules", {}).items()
}


# ---------------------------------------------------------------------------
# Overlay functions
# ---------------------------------------------------------------------------

def compute_seasonal_adjustment(product_id: str, month: int) -> float:
    """Return the seasonal % adjustment for a product in a given month.

    Args:
        product_id: The dashboard product_id (e.g. "<product>").
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
    """Propagate a feedstock price change through the causal chain.

    Args:
        product_id: The product whose forecast is being adjusted.
        naphtha_pct_change: The recent % change in feedstock price
            (e.g. 10.0 means feedstock rose 10%). None if no signal.

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
        naphtha_pct_change: Recent % change in feedstock price, or None.

    Returns:
        dict with keys:
            seasonal_pct: float — additive % from seasonal rules
            causal_pct: float — additive % from causal-chain propagation
            total_pct: float — seasonal_pct + causal_pct
            applied_rules: list[str] — human-readable rule identifiers
                          (e.g. "seasonal:<product>:12",
                           "causal:<product>:<root>=+10.0%")
    """
    applied_rules: list[str] = []

    seasonal_pct = compute_seasonal_adjustment(product_id, as_of_date.month)
    if seasonal_pct != 0.0:
        applied_rules.append(f"seasonal:{product_id}:{as_of_date.month}")

    causal_pct = compute_causal_chain_adjustment(product_id, naphtha_pct_change)
    if causal_pct != 0.0 and naphtha_pct_change is not None:
        sign = "+" if naphtha_pct_change >= 0 else ""
        applied_rules.append(
            f"causal:{product_id}:feedstock={sign}{naphtha_pct_change}%"
        )

    total_pct = round(seasonal_pct + causal_pct, 4)

    return {
        "seasonal_pct": seasonal_pct,
        "causal_pct": causal_pct,
        "total_pct": total_pct,
        "applied_rules": applied_rules,
    }


# ---------------------------------------------------------------------------
# Root-feedstock signal fetcher
# ---------------------------------------------------------------------------

_FEEDSTOCK_LOOKBACK_DAYS = 14
_FEEDSTOCK_RECENT_WINDOW = 3  # last 3 days vs preceding 11


def fetch_root_feedstock_pct_change(
    db: Any,
    org_id: str = "default-org",
    lookback_days: int = _FEEDSTOCK_LOOKBACK_DAYS,
) -> float | None:
    """Compute the recent % change in the root feedstock price.

    Reads the root-feedstock forecast target's source history (last 14 days) and
    compares the mean of the last 3 days against the mean of the preceding
    11 days. This is robust to single-day spikes.

    Args:
        db: SQLAlchemy session (sync).
        org_id: Org ID for the root-feedstock target.
        lookback_days: Total lookback window (default 14).

    Returns:
        The % change (e.g. 10.0 means feedstock rose 10%), or None if the
        root-feedstock target or its history is unavailable.
    """
    try:
        from app.models.forecasting import ForecastTarget
        from app.services.forecasting.mysql_data_source import MysqlDataSource

        target = db.query(ForecastTarget).filter(
            ForecastTarget.product_key == f"{_DOMAIN_SIGNALS_CFG.get('root_feedstock_key', '')}",
            ForecastTarget.org_id == org_id,
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).first()
        if target is None or not target.datasource:
            return None

        src = MysqlDataSource()
        df = src.read_history(target.datasource)
        if df is None or len(df) < lookback_days:
            return None

        # Take the last `lookback_days` rows
        recent = df.tail(lookback_days).reset_index(drop=True)
        if len(recent) <= _FEEDSTOCK_RECENT_WINDOW:
            return None
        recent_mean = recent.tail(_FEEDSTOCK_RECENT_WINDOW)["y"].mean()
        preceding_mean = recent.head(len(recent) - _FEEDSTOCK_RECENT_WINDOW)["y"].mean()

        if preceding_mean == 0 or preceding_mean is None or recent_mean is None:
            return None

        pct = round((recent_mean - preceding_mean) / preceding_mean * 100.0, 2)
        logger.info(
            "[domain-signals] feedstock %s: recent_mean=%.2f, preceding_mean=%.2f → %+.2f%%",
            org_id, recent_mean, preceding_mean, pct,
        )
        return pct
    except Exception as exc:
        logger.warning("[domain-signals] feedstock pct change fetch failed: %s", exc)
        return None
