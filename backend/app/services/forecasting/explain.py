"""NL explanation: driver attribution + forecast summaries (NO SHAP).

Uses feature_importances_ from XGBoost for top-N feature ranking.
Generates human-readable summaries with confidence levels.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DriverAttribution:
    feature: str
    weight: float  # 0-1


@dataclass
class Explanation:
    product_key: str
    summary: str
    confidence: str  # "high" | "medium" | "low"
    drivers: list[DriverAttribution]
    cleaning_note: str
    coherence_flags: list[str]
    drift_warning: bool
    regime: str = ""
    directional_signal: str = ""


def _extract_drivers(xgboost_model, feature_names: list[str]) -> list[DriverAttribution]:
    """Extract top-5 feature importances from a fitted XGBoost model."""
    if xgboost_model is None or not hasattr(xgboost_model, '_model') or xgboost_model._model is None or not feature_names:
        return []
    try:
        importances = xgboost_model._model.feature_importances_
        n_endo = len(importances) - len(feature_names)
        if n_endo <= 0:
            # All features are exogenous
            if len(importances) != len(feature_names):
                return []
            ranked = sorted(
                zip(feature_names, importances), key=lambda x: x[1], reverse=True
            )
        else:
            # Only look at exogenous portion (last len(feature_names) columns)
            exog_importances = importances[-len(feature_names):]
            ranked = sorted(
                zip(feature_names, exog_importances), key=lambda x: x[1], reverse=True
            )
        total = sum(w for _, w in ranked)
        drivers = [DriverAttribution(f, w / total) for f, w in ranked[:5]]
        return drivers
    except Exception as exc:
        logger.warning("Failed to extract feature importances: %s", exc)
        return []


def _compute_summary(
    product_key: str,
    forecast_values: list[float],
    previous_forecast: list[float] | None,
    xgboost_model,
    feature_names: list[str],
    cleaning_report,
    coherence_report,
    drift_status: dict,
    honesty_gate_triggered: bool,
) -> tuple[str, str, list[DriverAttribution], str, list[str], bool]:
    """Compute the Explanation fields."""
    # Direction + magnitude
    current_end = forecast_values[-1]
    trend_direction = "sideways"
    if len(forecast_values) >= 2:
        if forecast_values[-1] > forecast_values[0] * 1.02:
            trend_direction = "up"
        elif forecast_values[-1] < forecast_values[0] * 0.98:
            trend_direction = "down"

    # Compare vs previous
    revision_note = ""
    if previous_forecast and len(previous_forecast) == len(forecast_values):
        pct_change = (forecast_values[-1] - previous_forecast[-1]) / previous_forecast[-1] * 100
        direction = "upward" if pct_change >= 0 else "downward"
        revision_note = f" vs previous ({direction} revision {abs(pct_change):.1f}%)"

    # Driver attribution
    drivers = _extract_drivers(xgboost_model, feature_names)
    driver_text = ""
    if drivers:
        top = drivers[0]
        driver_text = f" Top driver: {top.feature} ({top.weight:.1%})."

    # Summary
    summary = (
        f"{product_key} forecast trending {trend_direction} (target: {current_end:.0f})"
        f"{revision_note}.{driver_text}"
    )

    # Confidence
    if drift_status.get("is_drifting") and honesty_gate_triggered:
        confidence = "low"
    elif drift_status.get("is_drifting") or honesty_gate_triggered:
        confidence = "medium"
    else:
        confidence = "high"

    # Cleaning note
    cleaning_note = ""
    if hasattr(cleaning_report, "notes"):
        cleaning_note = cleaning_report.notes

    # Coherence flags
    coherence_flags: list[str] = []
    if hasattr(coherence_report, "violations"):
        for v in coherence_report.violations:
            if isinstance(v, dict):
                coherence_flags.append(v.get("message", str(v)))

    drift_warning = drift_status.get("is_drifting", False)

    return summary, confidence, drivers, cleaning_note, coherence_flags, drift_warning


def explain_forecast(
    product_key: str,
    forecast_values: list[float],
    previous_forecast: list[float] | None,
    xgboost_model,
    feature_names: list[str],
    cleaning_report,
    coherence_report,
    drift_status: dict,
    honesty_gate_triggered: bool,
    regime: str = "",
    directional_signal: str = "",
) -> Explanation:
    """Produce a human-readable Explanation with driver attribution.

    Uses feature_importances_ from XGBoost. NO SHAP dependency.
    """
    summary, confidence, drivers, cleaning_note, coherence_flags, drift_warning = _compute_summary(
        product_key=product_key,
        forecast_values=forecast_values,
        previous_forecast=previous_forecast,
        xgboost_model=xgboost_model,
        feature_names=feature_names,
        cleaning_report=cleaning_report,
        coherence_report=coherence_report,
        drift_status=drift_status,
        honesty_gate_triggered=honesty_gate_triggered,
    )
    return Explanation(
        product_key=product_key,
        summary=summary,
        confidence=confidence,
        drivers=drivers,
        cleaning_note=cleaning_note,
        coherence_flags=coherence_flags,
        drift_warning=drift_warning,
        regime=regime,
        directional_signal=directional_signal,
    )
