"""Per-series weighted ensemble.

Each series gets its own model weights, computed by softmax over
negative backtest errors (MAPE or RMSE).  A floor weight ensures
every model gets a minimum share.  If a model crashes at fit-time,
it is dropped and weights are renormalized.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.services.forecasting.models.base import ForecastModel, ModelFitError

logger = logging.getLogger(__name__)

# Temperature for softmax: lower = more weight on best model
_DEFAULT_TAU = 1.0
# Floor weight — every model gets at least this share
_FLOOR_WEIGHT = 0.05


@dataclass
class EnsembleResult:
    """Result of per-series ensemble blending."""

    point_forecast: pd.Series  # blended forecast (h steps)
    weights: dict[str, float]  # model_name → weight (sum to 1)
    models_run: list[str]  # names of models that succeeded
    models_failed: list[str]  # names of models that crashed
    individual_forecasts: dict[str, pd.Series] = field(default_factory=dict)


def blend(
    forecasts: dict[str, pd.Series],
    per_model_error: dict[str, float],
    tau: float = _DEFAULT_TAU,
    floor: float = _FLOOR_WEIGHT,
) -> EnsembleResult:
    """Blend individual model forecasts using softmax-weighted average.

    Parameters
    ----------
    forecasts : dict[str, pd.Series]
        Model name → forecast series (all same length *h*).
    per_model_error : dict[str, float]
        Model name → backtest error (lower = better).  Models missing
        from this dict or with ``inf`` error are dropped.
    tau : float
        Softmax temperature.  Lower values concentrate weight on the
        best model.
    floor : float
        Minimum weight allocated to each model.  Weights are
        renormalized after applying the floor.

    Returns
    -------
    EnsembleResult
    """
    if not forecasts:
        raise ValueError("No forecasts to blend")

    # Determine horizon from the first forecast
    first = next(iter(forecasts.values()))
    h = len(first)

    # Filter: only models present in both dicts with finite error
    valid_names: list[str] = []
    valid_errors: list[float] = []
    for name, fc in forecasts.items():
        err = per_model_error.get(name, float("inf"))
        if err < float("inf") and len(fc) == h:
            valid_names.append(name)
            valid_errors.append(err)

    if not valid_names:
        # All models failed or had inf error — fall back to equal weight
        # on whatever forecasts we have
        sorted_names = sorted(forecasts.keys())
        n = len(sorted_names)
        w = 1.0 / n if n > 0 else 0.0
        weights = {name: w for name in sorted_names}
        blend_vals = _weighted_sum(forecasts, weights, h)
        individual = {
            name: fc for name, fc in forecasts.items() if len(fc) == h
        }
        return EnsembleResult(
            point_forecast=pd.Series(blend_vals, name="ensemble"),
            weights=weights,
            models_run=sorted_names,
            models_failed=[],
            individual_forecasts=individual,
        )

    # Softmax of negative errors
    errors_arr = np.array(valid_errors, dtype=float)
    neg_scaled = -errors_arr / (tau * max(np.mean(errors_arr), 1e-10))
    # Numerical stability: subtract max before exp
    neg_scaled -= neg_scaled.max()
    raw_weights = np.exp(neg_scaled)
    raw_weights /= raw_weights.sum()

    # Apply floor
    n_valid = len(valid_names)
    raw_weights = _apply_floor(raw_weights, floor, n_valid)
    weights = dict(zip(valid_names, raw_weights))

    # Blend
    blend_vals = _weighted_sum(forecasts, weights, h)

    # Identify crashed models
    all_model_names = set(forecasts.keys())
    models_failed = sorted(all_model_names - set(valid_names))

    individual = {
        name: fc for name, fc in forecasts.items()
        if name in valid_names and len(fc) == h
    }

    return EnsembleResult(
        point_forecast=pd.Series(blend_vals, name="ensemble"),
        weights=weights,
        models_run=sorted(valid_names),
        models_failed=models_failed,
        individual_forecasts=individual,
    )


def run_models(
    models: dict[str, ForecastModel],
    y: pd.Series,
    h: int,
    seasonal_period: int,
) -> tuple[dict[str, pd.Series], list[str], list[str]]:
    """Fit all models and produce forecasts.  Returns (forecasts, runs, failed)."""
    forecasts: dict[str, pd.Series] = {}
    models_run: list[str] = []
    models_failed: list[str] = []

    for name, model in models.items():
        try:
            model.fit(y, seasonal_period=seasonal_period)
            pred = model.forecast(h)
            forecasts[name] = pred
            models_run.append(name)
        except (ModelFitError, Exception) as exc:
            logger.info("Model %s failed: %s", name, exc)
            models_failed.append(name)

    return forecasts, models_run, models_failed


# ── helpers ────────────────────────────────────────────────────────────

def _apply_floor(
    weights: np.ndarray,
    floor: float,
    n: int,
) -> np.ndarray:
    """Apply floor weight, renormalize."""
    if n <= 1:
        return np.ones(1)
    # Floor per model
    w = np.maximum(weights, floor)
    # Renormalize
    total = w.sum()
    if total > 0:
        w /= total
    else:
        w = np.ones(n) / n
    return w


def _weighted_sum(
    forecasts: dict[str, pd.Series],
    weights: dict[str, float],
    h: int,
) -> np.ndarray:
    """Weighted sum of forecast arrays, padded to length *h*."""
    result = np.zeros(h, dtype=float)
    weight_total = 0.0

    for name, w in weights.items():
        fc = forecasts.get(name)
        if fc is None:
            continue
        arr = np.array(fc.values[:h], dtype=float)
        if len(arr) < h:
            padded = np.full(h, arr[-1] if len(arr) > 0 else 0.0)
            padded[:len(arr)] = arr
            arr = padded
        result += w * arr
        weight_total += w

    if weight_total > 0:
        result /= weight_total
    return result
