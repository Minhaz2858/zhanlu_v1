"""Value-chain coherence: spread guardrails + directional consistency.

Not literal HTS reconciliation — our crude→naphtha→C5 chain is a production
chain with yield rates, not an additive disaggregation. Instead, we check:
  1. Spread guardrails — child forecast should not invert vs feedstock cost
  2. Directional consistency — flag if feedstock moved >X% but child didn't
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CoherenceReport:
    product_key: str
    feedstock_key: str | None
    violations: list[dict] = field(default_factory=list)
    direction_consistent: bool = True
    spread_inverted: bool = False
    clamped: bool = False


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
