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
# Floor weight — every model gets at least this share (0.0 = no floor;
# bad models are gated out by _MAX_ERROR_RATIO instead).
_FLOOR_WEIGHT = 0.0
# Models with error > _MAX_ERROR_RATIO * best_error get zero weight
# (prevents bad models from polluting the blend).
_MAX_ERROR_RATIO = 2.0

# Regime-aware weight multipliers applied BEFORE softmax normalization.
# These boost/dampen model weights based on market state.
_REGIME_WEIGHT_MULT: dict[str, dict[str, float]] = {
    "bull": {
        "xgboost_reg": 1.3,
        "xgboost_exog": 1.3,
        "ets": 1.2,
        "mean_reversion": 0.7,
        "naive_last": 0.8,
    },
    "bear": {
        "mean_reversion": 1.3,
        "naive_last": 1.2,
        "seasonal_naive": 1.1,
        "xgboost_reg": 0.8,
        "xgboost_exog": 0.8,
        "ets": 0.8,
    },
    "volatile": {
        "naive_last": 1.5,
        "seasonal_naive": 1.3,
        "mean_reversion": 1.2,
        "xgboost_reg": 0.6,
        "xgboost_exog": 0.6,
        "stl": 0.7,
    },
    "sideways": {
        # No adjustment — equal opportunity
    },
}


@dataclass
class EnsembleResult:
    """Result of per-series ensemble blending."""

    point_forecast: pd.Series  # blended forecast (h steps)
    # model_name -> weight.  float (flat) when no per-horizon errors are
    # provided; list[float] (per-step) when per_model_error_by_horizon is used.
    weights: dict[str, float | list[float]]
    models_run: list[str]  # names of models that succeeded
    models_failed: list[str]  # names of models that crashed
    individual_forecasts: dict[str, pd.Series] = field(default_factory=dict)


def auto_tune_tau(
    per_model_mape: dict[str, float],
    tau_min: float = 0.5,
    tau_max: float = 3.0,
) -> float:
    """Compute optimal softmax temperature from backtest error variance.

    - Low variance  (models agree)   → low tau  (concentrate on best model).
    - High variance (models disagree) → high tau (diversify across models).

    Returns a float in [tau_min, tau_max]; falls back to _DEFAULT_TAU when
    fewer than 2 finite error values are available.
    """
    import math

    values: list[float] = []
    for v in per_model_mape.values():
        if v is not None and math.isfinite(v) and v >= 0:
            values.append(v)

    if len(values) < 2:
        return _DEFAULT_TAU

    mean_v = max(float(np.mean(values)), 1e-10)
    cv = float(np.std(values)) / mean_v
    tau_raw = 0.5 + 1.5 * cv
    return max(tau_min, min(tau_max, tau_raw))


def blend(
    forecasts: dict[str, pd.Series],
    per_model_error: dict[str, float],
    tau: float = _DEFAULT_TAU,
    floor: float = _FLOOR_WEIGHT,
    per_model_error_by_horizon: dict[int, dict[str, float]] | None = None,
    regime: str | None = None,
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
        Minimum weight allocated to each model (default 0.0 — ratio
        gating handles bad-model exclusion instead).
    per_model_error_by_horizon : dict[int, dict[str, float]] or None
        When provided, computes **per-step** weights: for forecast step
        ``t`` (0-indexed), the nearest horizon ``h* >= t+1`` is selected
        and its per-model errors drive the softmax for that step.
        ``weights`` becomes ``dict[str, list[float]]``.  When None,
        flat weights (``dict[str, float]``) are produced.

    Returns
    -------
    EnsembleResult
    """
    if not forecasts:
        raise ValueError("No forecasts to blend")

    # Determine horizon from the first forecast
    first = next(iter(forecasts.values()))
    h = len(first)

    # Filter: only models present in both dicts with finite error + correct length
    valid_names: list[str] = []
    valid_errors: list[float] = []
    for name, fc in forecasts.items():
        err = per_model_error.get(name, float("inf"))
        if err < float("inf") and len(fc) == h:
            valid_names.append(name)
            valid_errors.append(err)

    if not valid_names:
        # All models failed or had inf error — fall back to equal weight
        sorted_names = sorted(forecasts.keys())
        n = len(sorted_names)
        w = 1.0 / n if n > 0 else 0.0
        if per_model_error_by_horizon:
            weights = {name: [w] * h for name in sorted_names}
        else:
            weights = {name: w for name in sorted_names}
        blend_vals = _weighted_sum(forecasts, {name: w for name in sorted_names}, h)
        individual = {name: fc for name, fc in forecasts.items() if len(fc) == h}
        return EnsembleResult(
            point_forecast=pd.Series(blend_vals, name="ensemble"),
            weights=weights,
            models_run=sorted_names,
            models_failed=[],
            individual_forecasts=individual,
        )

    # All valid names (finite flat error + correct length) — kept for the
    # weights dict so gated-out models appear with 0 weight.
    all_valid_names = list(valid_names)

    # Ratio gating: models whose error > _MAX_ERROR_RATIO * best get 0 weight.
    best_err = min(valid_errors)
    gated_names = [n for n, e in zip(valid_names, valid_errors) if e <= best_err * _MAX_ERROR_RATIO]
    if not gated_names:
        gated_names = [valid_names[valid_errors.index(best_err)]]
    gated_errors = [per_model_error[n] for n in gated_names]

    # ── Per-horizon path: per-step weights ─────────────────────────────
    if per_model_error_by_horizon:
        sorted_horizons = sorted(per_model_error_by_horizon.keys())
        per_step_weights: dict[str, list[float]] = {name: [0.0] * h for name in all_valid_names}
        blend_vals = np.zeros(h, dtype=float)

        for t in range(h):
            # Nearest horizon >= t+1 (step t is the (t+1)-step-ahead forecast)
            h_star = next((hh for hh in sorted_horizons if hh >= t + 1), sorted_horizons[-1])
            herr_map = per_model_error_by_horizon.get(h_star, {})
            # Use gated names; per-horizon error falls back to flat error.
            step_errs = [herr_map.get(n, per_model_error.get(n, float("inf"))) for n in gated_names]
            finite = [(n, e) for n, e in zip(gated_names, step_errs) if np.isfinite(e)]
            if finite:
                step_names = [n for n, _ in finite]
                step_errs = [e for _, e in finite]
            else:
                step_names = gated_names
                step_errs = gated_errors
            step_w = _softmax(step_errs, tau)
            step_w = _apply_regime_multipliers(step_names, step_w, regime)
            for n, wv in zip(step_names, step_w):
                per_step_weights[n][t] = float(wv)
                arr = np.asarray(forecasts[n].values, dtype=float)
                blend_vals[t] += wv * (arr[t] if t < len(arr) else arr[-1])

        models_failed = sorted(set(forecasts.keys()) - set(all_valid_names))
        individual = {name: fc for name, fc in forecasts.items() if name in all_valid_names and len(fc) == h}

        return EnsembleResult(
            point_forecast=pd.Series(blend_vals, name="ensemble"),
            weights=per_step_weights,
            models_run=sorted(all_valid_names),
            models_failed=models_failed,
            individual_forecasts=individual,
        )

    # ── Flat path (backward compat) ────────────────────────────────────
    raw_weights = _softmax(gated_errors, tau)
    raw_weights = _apply_floor(raw_weights, floor, len(gated_names))
    raw_weights = _apply_regime_multipliers(gated_names, raw_weights, regime)
    weights: dict[str, float | list[float]] = {name: 0.0 for name in all_valid_names}
    for n, wv in zip(gated_names, raw_weights):
        weights[n] = float(wv)
    blend_vals = _weighted_sum(forecasts, {n: weights[n] for n in gated_names}, h)

    models_failed = sorted(set(forecasts.keys()) - set(all_valid_names))
    individual = {name: fc for name, fc in forecasts.items() if name in all_valid_names and len(fc) == h}

    return EnsembleResult(
        point_forecast=pd.Series(blend_vals, name="ensemble"),
        weights=weights,
        models_run=sorted(all_valid_names),
        models_failed=models_failed,
        individual_forecasts=individual,
    )


def run_models(
    models: dict[str, ForecastModel],
    y: pd.Series,
    h: int,
    seasonal_period: int,
    product_key: str = "",
) -> tuple[dict[str, pd.Series], list[str], list[str]]:
    """Fit all models and produce forecasts.  Returns (forecasts, runs, failed)."""
    forecasts: dict[str, pd.Series] = {}
    models_run: list[str] = []
    models_failed: list[str] = []

    for name, model in models.items():
        try:
            model.fit(y, seasonal_period=seasonal_period, product_key=product_key)
            pred = model.forecast(h)
            forecasts[name] = pred
            models_run.append(name)
        except (ModelFitError, Exception) as exc:
            logger.info("Model %s failed: %s", name, exc)
            models_failed.append(name)

    return forecasts, models_run, models_failed


# ── helpers ────────────────────────────────────────────────────────────

def _softmax(errors: list[float], tau: float) -> np.ndarray:
    """Softmax of negative errors.  Lower error -> higher weight."""
    errors_arr = np.array(errors, dtype=float)
    neg_scaled = -errors_arr / (tau * max(np.mean(errors_arr), 1e-10))
    neg_scaled -= neg_scaled.max()  # numerical stability
    raw = np.exp(neg_scaled)
    raw /= raw.sum()
    return raw


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


def _apply_regime_multipliers(
    model_names: list[str],
    weights: np.ndarray,
    regime: str | None,
) -> np.ndarray:
    """Apply regime-based weight multipliers, then re-normalize."""
    if not regime or regime not in _REGIME_WEIGHT_MULT:
        return weights

    mults = _REGIME_WEIGHT_MULT[regime]
    adjusted = weights.copy()
    for i, name in enumerate(model_names):
        for pattern, mult in mults.items():
            if pattern in name:
                adjusted[i] *= mult
                break

    total = adjusted.sum()
    if total > 1e-15:
        adjusted /= total
    return adjusted


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
