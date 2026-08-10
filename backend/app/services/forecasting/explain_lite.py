"""P3-3: SHAP-lite explainability — permutation importance with directional attribution.

Provides per-prediction driver direction (does shuffling this feature push the
forecast UP or DOWN?) without pulling in the `shap` package.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class DirectionalDriver:
    """A single feature's contribution direction and magnitude."""

    feature: str
    weight: float            # Permutation importance magnitude (0-1 normalized)
    direction: str           # "up", "down", or "neutral"
    mean_forecast_delta: float  # Raw forecast change when feature shuffled


@dataclass
class ExplanationLite:
    """Lightweight explanation with directional drivers."""

    product_key: str
    summary: str
    confidence: str           # "High"/"Medium"/"Low"
    drivers: list[DirectionalDriver] = field(default_factory=list)
    top_n: int = 5


def permutation_importance(
    model: object,
    X: pd.DataFrame,
    feature_names: list[str],
    forecast_fn,
    n_repeats: int = 3,
    baseline_forecast: np.ndarray | None = None,
) -> list[DirectionalDriver]:
    """Compute permutation importance with directional attribution.

    For each feature, shuffles its column n_repeats times and measures
    how much the forecast changes on average. Direction is determined
    by whether shuffling tends to increase or decrease the forecast.

    Args:
        model: Trained model with .predict().
        X: Feature matrix (DataFrame).
        feature_names: List of feature column names.
        forecast_fn: Callable(model, X) -> np.ndarray producing the forecast.
        n_repeats: Number of shuffle repetitions.
        baseline_forecast: Pre-computed forecast on unshuffled X.

    Returns:
        List of DirectionalDriver sorted by absolute weight descending.
    """
    if baseline_forecast is None:
        baseline_forecast = forecast_fn(model, X)

    baseline_mean = float(np.mean(baseline_forecast))
    drivers: list[DirectionalDriver] = []

    for feat in feature_names:
        if feat not in X.columns:
            continue
        deltas: list[float] = []
        for _ in range(n_repeats):
            X_shuffled = X.copy()
            # Shuffle this feature's column
            col_vals = X_shuffled[feat].values.copy()
            np.random.shuffle(col_vals)
            X_shuffled[feat] = col_vals
            fc_shuffled = forecast_fn(model, X_shuffled)
            delta = float(np.mean(fc_shuffled - baseline_forecast))
            deltas.append(delta)

        mean_delta = float(np.mean(deltas))
        # Direction: if shuffling increases forecast, the feature has a
        # downward pull (removing it pushes forecast up). If shuffling
        # decreases forecast, the feature has an upward pull.
        if abs(mean_delta) < 1e-6:
            direction = "neutral"
        elif mean_delta > 0:
            direction = "down"   # Feature suppresses forecast
        else:
            direction = "up"     # Feature boosts forecast

        weight = abs(mean_delta)  # Raw weight
        drivers.append(DirectionalDriver(
            feature=feat, weight=weight,
            direction=direction,
            mean_forecast_delta=round(mean_delta, 6),
        ))

    # Normalize weights to 0-1
    if drivers:
        max_w = max(d.weight for d in drivers)
        if max_w > 0:
            for d in drivers:
                d.weight = round(d.weight / max_w, 4)

    # Sort by weight descending
    drivers.sort(key=lambda d: d.weight, reverse=True)
    return drivers


def explain_forecast_lite(
    model: object,
    X: pd.DataFrame,
    feature_names: list[str],
    forecast_fn,
    product_key: str = "",
    top_n: int = 5,
) -> ExplanationLite:
    """Produce a lightweight directional explanation.

    Args:
        model: Trained model.
        X: Feature matrix.
        feature_names: Column names.
        forecast_fn: model.predict equivalent.
        product_key: Product identifier.
        top_n: Number of top drivers to include.

    Returns:
        ExplanationLite with sorted directional drivers.
    """
    drivers = permutation_importance(model, X, feature_names, forecast_fn)

    top_drivers = [d for d in drivers if d.weight > 0][:top_n]

    # Summary sentence
    if not top_drivers:
        summary = "No significant feature drivers found."
        confidence = "Low"
    else:
        up_features = [d.feature for d in top_drivers if d.direction == "up"][:3]
        down_features = [d.feature for d in top_drivers if d.direction == "down"][:3]
        parts = []
        if up_features:
            parts.append(f"Upward: {', '.join(up_features)}")
        if down_features:
            parts.append(f"Downward: {', '.join(down_features)}")
        summary = " | ".join(parts) if parts else "Feature drivers identified."
        confidence = "High" if len(top_drivers) >= 3 else "Medium"

    return ExplanationLite(
        product_key=product_key,
        summary=summary,
        confidence=confidence,
        drivers=top_drivers,
        top_n=top_n,
    )
