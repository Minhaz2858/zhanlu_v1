"""Shared what-if simulation helper for forecast products.

Extracted from ``app.routers.forecast_ops.get_what_if_simulation`` so both the
HTTP endpoint and the agent tool (``forecast_what_if``) can reuse the same
causal-chain elasticity math without model re-running.
"""
from datetime import datetime, timezone

from app.models.forecasting import ForecastRun, ForecastTarget
from app.services.domain_config import get_domain_config
from app.services.forecasting.domain_signals import compute_domain_signal_adjustment

# Display label for the root-feedstock shock driver — from the app's domain
# config ("root_feedstock_label"). Empty config = generic label.
_ROOT_FEEDSTOCK_LABEL: str = (get_domain_config("") or {}).get(
    "root_feedstock_label"
) or "feedstock"


def _point_forecast_from_run(run) -> list:
    """Extract the base forecast series from a ``ForecastRun``.

    The model stores forecast curves in ``results`` JSON as
    ``{str(h): {"base": [...], "bull": [...], "bear": [...]}}``.
    Returns the longest available base series (the full forecast curve),
    or an empty list when nothing usable is present.
    """
    results = run.results or {}
    if not isinstance(results, dict):
        return []
    best = None
    for payload in results.values():
        if not isinstance(payload, dict):
            continue
        base = payload.get("base")
        if not base:
            continue
        if isinstance(base, (list, tuple)):
            vals = [float(v) for v in base]
        else:  # scalar (per model docstring) — treat as single-point series
            vals = [float(base)]
        if best is None or len(vals) > len(best):
            best = vals
    return best or []


def compute_what_if(
    product_key: str,
    market_delta_pct: float,
    feedstock_delta_pct: float,
    db,
) -> dict:
    """Simulate forecast changes given upstream price shocks.

    Uses domain_signals causal-chain elasticities for instant propagation
    (no model re-running required).

    Parameters
    ----------
    product_key : str
        Product to simulate.
    market_delta_pct : float
        Percentage change in the market index (e.g., 5.0 = +5%).
    feedstock_delta_pct : float
        Percentage change in the root feedstock price (e.g., -3.0 = -3%).
    db
        SQLAlchemy session.

    Returns
    -------
    dict
        { product_key, base_forecast, adjusted_forecast, adjustments: [
          { driver, delta_pct, impact_pct, adjusted_value }
        ]}
    """
    target = db.query(ForecastTarget).filter_by(
        product_key=product_key, is_deleted=False,
    ).first()
    if target is None:
        raise LookupError(f"no forecast target for '{product_key}'")

    # Get latest forecast
    latest_run = (
        db.query(ForecastRun)
        .filter(
            ForecastRun.target_id == target.id,
            ForecastRun.results.isnot(None),
        )
        .order_by(ForecastRun.created_date.desc())
        .first()
    )

    base_forecast = _point_forecast_from_run(latest_run) if latest_run else []
    if not base_forecast:
        return {
            "product_key": product_key,
            "message": "No forecast available for simulation",
            "adjustments": [],
        }

    # Compute adjustments from upstream shocks
    adjustments = []
    total_impact = 0.0

    # Root-feedstock shock propagation
    if feedstock_delta_pct != 0.0:
        feedstock_adj = compute_domain_signal_adjustment(
            product_id=product_key,
            as_of_date=datetime.now(timezone.utc),
            naphtha_pct_change=feedstock_delta_pct,
        )
        impact = feedstock_adj.get("causal_pct", 0.0)
        total_impact += impact
        adjustments.append({
            "driver": _ROOT_FEEDSTOCK_LABEL,
            "delta_pct": feedstock_delta_pct,
            "impact_pct": round(impact, 2),
            "description": (
                f"{_ROOT_FEEDSTOCK_LABEL.capitalize()} {feedstock_delta_pct:+.1f}% "
                f"→ {impact:+.2f}% impact"
            ),
        })

    # External market shock (simplified: treat as direct root-feedstock proxy)
    if market_delta_pct != 0.0:
        # The market index typically affects the root feedstock with ~0.8 elasticity
        market_to_feedstock = market_delta_pct * 0.8
        market_adj = compute_domain_signal_adjustment(
            product_id=product_key,
            as_of_date=datetime.now(timezone.utc),
            naphtha_pct_change=market_to_feedstock,
        )
        impact = market_adj.get("causal_pct", 0.0)
        total_impact += impact
        adjustments.append({
            "driver": "market_index",
            "delta_pct": market_delta_pct,
            "impact_pct": round(impact, 2),
            "description": (
                f"Market {market_delta_pct:+.1f}% → {_ROOT_FEEDSTOCK_LABEL} "
                f"{market_to_feedstock:+.1f}% → {impact:+.2f}% impact"
            ),
        })

    # Apply total impact to base forecast
    adjusted_forecast = [
        round(v * (1 + total_impact / 100), 2)
        for v in base_forecast
    ]

    return {
        "product_key": product_key,
        "base_forecast": base_forecast,
        "adjusted_forecast": adjusted_forecast,
        "total_impact_pct": round(total_impact, 2),
        "adjustments": adjustments,
    }
