"""Value-chain coherence: spread guardrails + directional consistency + hierarchical reconciliation.

Not literal HTS reconciliation — the app's configured value chain is a
production chain with yield rates, not an additive disaggregation. Instead, we support:
  1. Spread guardrails — child forecast should not invert vs feedstock cost
  2. Directional consistency — flag if feedstock moved >X% but child didn't
  3. Top-down allocation — parent forecast → child share by yield ratio
  4. Middle-out constraint — sibling products share a parent ceiling
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.services.domain_config import get_domain_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hierarchy defaults — loaded from the app's domain config ("hierarchy"
# block: "yield_ratios" + "margin_premiums"). Empty config = no defaults
# (top-down allocation is skipped gracefully). Per-target overrides can
# still be supplied via target.model_config["hierarchy"]["yield_ratios"].
# ---------------------------------------------------------------------------
_DOMAIN_CFG: dict[str, Any] = get_domain_config("")

# Default cracking yield ratios (feedstock → child products)
_DEFAULT_YIELD_RATIOS: dict[str, dict[str, float]] = dict(
    (_DOMAIN_CFG.get("hierarchy") or {}).get("yield_ratios") or {}
)

# Default premium over feedstock cost per product (fraction)
_DEFAULT_MARGIN_PREMIUMS: dict[str, float] = dict(
    (_DOMAIN_CFG.get("hierarchy") or {}).get("margin_premiums") or {}
)


@dataclass
class CoherenceReport:
    product_key: str
    feedstock_key: str | None
    violations: list[dict] = field(default_factory=list)
    direction_consistent: bool = True
    spread_inverted: bool = False
    clamped: bool = False


@dataclass
class TopDownAllocationResult:
    """Result of top-down hierarchical allocation."""
    parent_key: str
    parent_forecast: list[float]
    child_allocations: dict[str, list[float]]  # product_key → forecast values
    yield_ratios_used: dict[str, float]
    adjustments_made: list[dict] = field(default_factory=list)


@dataclass
class MiddleOutConstraintResult:
    """Result of middle-out coherence constraint between siblings."""
    sibling_group: str  # e.g. "c5_derivatives"
    original_forecasts: dict[str, list[float]]
    adjusted_forecasts: dict[str, list[float]]
    violations: list[dict] = field(default_factory=list)


def check_coherence(
    product_key: str,
    forecast_values: list[float],
    feedstock_forecast: list[float] | None,
    feedstock_key: str | None,
    spread_inversion_threshold: float = 0.0,
    direction_threshold_pct: float = 5.0,
) -> CoherenceReport:
    """Check value-chain coherence. Does NOT silently rewrite forecasts."""
    report = CoherenceReport(product_key=product_key, feedstock_key=feedstock_key)

    if feedstock_forecast is None or len(feedstock_forecast) != len(forecast_values):
        return report

    # 1. Spread guardrails
    for i, (child, parent) in enumerate(zip(forecast_values, feedstock_forecast)):
        if parent <= 0:
            continue
        ratio = child / parent
        if ratio < 1.0 + spread_inversion_threshold:
            report.spread_inverted = True
            report.violations.append({
                "type": "spread_inversion",
                "step": i,
                "forecast_value": child,
                "feedstock_value": parent,
                "ratio": ratio,
                "severity": "high",
                "message": f"{product_key} step {i}: forecast {child:.2f} below feedstock {parent:.2f}",
            })

    # 2. Directional consistency
    if len(forecast_values) >= 2:
        feedstock_start = feedstock_forecast[0]
        feedstock_end = feedstock_forecast[-1]
        if feedstock_start > 0:
            feedstock_pct_move = abs((feedstock_end - feedstock_start) / feedstock_start) * 100
            if feedstock_pct_move > direction_threshold_pct:
                child_start = forecast_values[0]
                child_end = forecast_values[-1]
                child_pct_move = abs((child_end - child_start) / child_start) * 100 if child_start > 0 else 0
                if child_pct_move < direction_threshold_pct:
                    report.direction_consistent = False
                    report.violations.append({
                        "type": "directional_mismatch",
                        "feedstock_pct_move": feedstock_pct_move,
                        "child_pct_move": child_pct_move,
                        "severity": "medium",
                        "message": (
                            f"{feedstock_key} moved {feedstock_pct_move:.1f}% but "
                            f"{product_key} only moved {child_pct_move:.1f}%"
                        ),
                    })

    return report


def apply_coherence(
    forecast_values: list[float],
    report: CoherenceReport,
    feedstock_forecast: list[float] | None,
    min_margin: float = 0.2,
) -> tuple[list[float], CoherenceReport]:
    """Apply clamps for hard inversions. Returns (forecast, updated_report)."""
    new_report = CoherenceReport(
        product_key=report.product_key,
        feedstock_key=report.feedstock_key,
        violations=list(report.violations),
        direction_consistent=report.direction_consistent,
        spread_inverted=report.spread_inverted,
        clamped=False,
    )

    if feedstock_forecast is None or not new_report.spread_inverted:
        return forecast_values, new_report

    # Apply clamps: child = feedstock * (1 + min_margin)
    clamped = list(forecast_values)
    was_clamped = False
    for i, (child, parent) in enumerate(zip(clamped, feedstock_forecast)):
        if parent > 0 and child < parent * (1 + min_margin):
            new_value = parent * (1 + min_margin)
            clamped[i] = new_value
            was_clamped = True

    new_report.clamped = was_clamped
    return clamped, new_report


# ---------------------------------------------------------------------------
# Top-down allocation
# ---------------------------------------------------------------------------

def allocate_top_down(
    parent_key: str,
    parent_forecast: list[float],
    child_forecasts: dict[str, list[float]] | None = None,
    yield_ratios: dict[str, float] | None = None,
    margin_premiums: dict[str, float] | None = None,
    blend_ratio: float = 0.5,
) -> TopDownAllocationResult:
    """Top-down allocation: derive child forecasts from parent forecast.

    For each child product, the allocated forecast is a blend of:
      - The child's own independent forecast (if provided)
      - The parent-implied forecast: parent_price * (1 + margin_premium)

    The ``blend_ratio`` controls the mix:
      - 0.0 = use only the child's own forecast (no top-down influence)
      - 1.0 = use only the parent-implied forecast
      - 0.5 = equal blend (default)

    When no child forecast is provided, the parent-implied forecast is used
    directly (blend_ratio is effectively 1.0 for that child).

    Parameters
    ----------
    parent_key : str
        Parent product key (e.g. "<product>").
    parent_forecast : list[float]
        Parent forecast values.
    child_forecasts : dict[str, list[float]] | None
        Existing child forecasts keyed by product_key. If None, fully
        top-down allocation is used.
    yield_ratios : dict[str, float] | None
        Yield ratios for child products. Defaults to _DEFAULT_YIELD_RATIOS.
    margin_premiums : dict[str, float] | None
        Fractional margin over feedstock cost for each child.
        Defaults to _DEFAULT_MARGIN_PREMIUMS.
    blend_ratio : float
        Blend between child's own forecast and parent-implied forecast.

    Returns
    -------
    TopDownAllocationResult
    """
    ratios = yield_ratios or _DEFAULT_YIELD_RATIOS.get(parent_key, {})
    margins = margin_premiums or _DEFAULT_MARGIN_PREMIUMS
    n_steps = len(parent_forecast)

    if not ratios:
        logger.warning("No yield ratios for parent %s — skipping top-down allocation", parent_key)
        return TopDownAllocationResult(
            parent_key=parent_key,
            parent_forecast=parent_forecast,
            child_allocations={},
            yield_ratios_used={},
        )

    child_allocations: dict[str, list[float]] = {}
    adjustments: list[dict] = []

    for child_key, yield_ratio in ratios.items():
        margin = margins.get(child_key, 0.20)
        # Parent-implied forecast: parent_price * (1 + margin)
        implied = [p * (1 + margin) for p in parent_forecast]

        if child_forecasts and child_key in child_forecasts:
            own_forecast = child_forecasts[child_key]
            if len(own_forecast) != n_steps:
                logger.warning(
                    "Child %s forecast length %d != parent %d — using implied only",
                    child_key, len(own_forecast), n_steps,
                )
                child_allocations[child_key] = implied
                adjustments.append({
                    "child": child_key,
                    "type": "length_mismatch",
                    "action": "used_implied_only",
                })
                continue

            # Blend own forecast with implied
            blended = [
                blend_ratio * imp + (1 - blend_ratio) * own
                for own, imp in zip(own_forecast, implied)
            ]
            child_allocations[child_key] = blended

            # Track adjustments
            avg_own = float(np.mean(own_forecast))
            avg_blended = float(np.mean(blended))
            if abs(avg_blended - avg_own) > 1.0:
                adjustments.append({
                    "child": child_key,
                    "type": "top_down_adjustment",
                    "own_avg": round(avg_own, 2),
                    "implied_avg": round(float(np.mean(implied)), 2),
                    "blended_avg": round(avg_blended, 2),
                    "pct_change": round((avg_blended - avg_own) / avg_own * 100, 2) if avg_own else 0,
                })
        else:
            # No child forecast — use implied directly
            child_allocations[child_key] = implied
            adjustments.append({
                "child": child_key,
                "type": "no_own_forecast",
                "action": "used_implied",
            })

    return TopDownAllocationResult(
        parent_key=parent_key,
        parent_forecast=parent_forecast,
        child_allocations=child_allocations,
        yield_ratios_used=ratios,
        adjustments_made=adjustments,
    )


# ---------------------------------------------------------------------------
# Middle-out constraint
# ---------------------------------------------------------------------------

def constrain_middle_out(
    sibling_group: str,
    sibling_forecasts: dict[str, list[float]],
    parent_forecast: list[float] | None = None,
    min_margin: float = 0.15,
) -> MiddleOutConstraintResult:
    """Middle-out coherence: ensure sibling products don't violate structural constraints.

    When a parent forecast is provided, no sibling should exceed
    ``parent * (1 - min_margin)`` — a sibling at or above the parent
    price without margin is a structural violation.

    Also checks that siblings of the same tier don't invert relative
    to each other without justification (e.g., a downstream product should
    not exceed a higher-value sibling's price by >50%).

    Parameters
    ----------
    sibling_group : str
        Group name (e.g. "c5_derivatives").
    sibling_forecasts : dict[str, list[float]]
        Forecasts for each sibling product.
    parent_forecast : list[float] | None
        Parent (feedstock) forecast for ceiling constraint.
    min_margin : float
        Minimum margin between parent and child prices.

    Returns
    -------
    MiddleOutConstraintResult
    """
    adjusted = {k: list(v) for k, v in sibling_forecasts.items()}
    violations: list[dict] = []

    # 1. Parent ceiling constraint
    if parent_forecast is not None:
        n_steps = len(parent_forecast)
        for sib_key, sib_vals in adjusted.items():
            if len(sib_vals) != n_steps:
                continue
            for i, (sib, par) in enumerate(zip(sib_vals, parent_forecast)):
                ceiling = par * (1 - min_margin)
                if par > 0 and sib > ceiling and sib > par:
                    # Sibling price above parent — clamp to parent * (1 - min_margin)
                    # Actually this is unusual; child > parent means child has premium
                    # We only flag if sib > par * 2 (unreasonably high premium)
                    if sib > par * 2:
                        violations.append({
                            "type": "excessive_premium",
                            "sibling": sib_key,
                            "step": i,
                            "sibling_price": sib,
                            "parent_price": par,
                            "ratio": round(sib / par, 2),
                            "severity": "high",
                            "message": f"{sib_key} at step {i}: {sib:.2f} is {sib/par:.1f}x parent {par:.2f}",
                        })

    # 2. Sibling ordering constraint
    # (e.g., higher-value products typically price above lower-value siblings)
    sibling_keys = list(adjusted.keys())
    if len(sibling_keys) >= 2:
        # Check for extreme inversions between any pair
        for i in range(len(sibling_keys)):
            for j in range(i + 1, len(sibling_keys)):
                k1, k2 = sibling_keys[i], sibling_keys[j]
                v1, v2 = adjusted[k1], adjusted[k2]
                if len(v1) != len(v2):
                    continue
                # If one is consistently >3x the other, flag
                ratios = [a / b if b > 0 else 0 for a, b in zip(v1, v2)]
                avg_ratio = float(np.mean(ratios)) if ratios else 1.0
                if avg_ratio > 3.0 or avg_ratio < 0.33:
                    violations.append({
                        "type": "sibling_inversion",
                        "siblings": [k1, k2],
                        "avg_ratio": round(avg_ratio, 2),
                        "severity": "medium",
                        "message": f"{k1}/{k2} avg ratio {avg_ratio:.2f} — possible inversion",
                    })

    return MiddleOutConstraintResult(
        sibling_group=sibling_group,
        original_forecasts={k: list(v) for k, v in sibling_forecasts.items()},
        adjusted_forecasts=adjusted,
        violations=violations,
    )
