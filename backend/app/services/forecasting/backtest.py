"""Walk-forward backtest evaluation (Phase C rewrite).

For each model in the pool, runs a **sliding-origin** walk-forward
backtest: trains on ``y[:T]``, forecasts ``h_max`` steps, compares
against actuals at each requested horizon.  Produces per-horizon MAPE,
per-step residuals (fixes the old dead-code bug where ``all_residuals``
was declared but never populated), and directional accuracy.

Backward-compatible fields (``per_model_mape``, ``ensemble_mape``,
``naive_mape``, ``residuals``, ``metric``) are preserved so existing
callers — including ``engine.py`` and ``scenarios.py`` — keep working.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.services.forecasting.models.base import ForecastModel

logger = logging.getLogger(__name__)

# Minimum number of observations required in the first training fold.
_MIN_TRAIN_SIZE = 30
# Default horizons evaluated (days).
_DEFAULT_HORIZONS = [7, 14, 30]
# Sliding-origin step (days between consecutive origins).
_DEFAULT_STEP = 14
# Maximum backtest folds (cap to bound runtime on long series).
_MAX_FOLDS = 10


@dataclass
class BacktestResult:
    """Result of walk-forward evaluation for one series."""

    per_model_mape: dict[str, float]  # backward-compat: avg across horizons
    ensemble_mape: float
    naive_mape: float  # seasonal_naive MAPE (the honesty gate benchmark)
    n_folds: int
    residuals: list[float]  # backward-compat: flattened residuals at primary horizon
    metric: str = "MAPE"  # "MAPE" or "RMSE" (RMSE used when actuals contain zeros)
    # NEW (Phase C):
    per_horizon_mape: dict[int, dict[str, float]] = field(default_factory=dict)
    residuals_by_horizon: dict[int, list[float]] = field(default_factory=dict)
    directional_accuracy: dict[str, float] = field(default_factory=dict)
    # Phase A foundation-model metrics:
    per_model_mase: dict[str, float] = field(default_factory=dict)
    per_model_crps: dict[str, float | None] = field(default_factory=dict)
    interval_coverage: dict[str, float | None] = field(default_factory=dict)
    interval_width: dict[str, float | None] = field(default_factory=dict)


def evaluate(
    y: pd.Series,
    models: dict[str, ForecastModel],
    seasonal_period: int = 7,
    min_train: int = _MIN_TRAIN_SIZE,
    min_holdout: int = 3,
    max_folds: int = _MAX_FOLDS,
    horizons: list[int] | None = None,
    step: int = _DEFAULT_STEP,
) -> BacktestResult:
    """Evaluate all models via sliding-origin walk-forward holdout.

    Parameters
    ----------
    y : pd.Series
        Full time series (NaNs dropped before use).
    models : dict[str, ForecastModel]
        Model pool to evaluate.  Crashing models are skipped.
    seasonal_period : int
        Passed to models that need a seasonal period.
    min_train : int
        Minimum observations in the first training split.
    min_holdout : int
        Reserved for backward-compat signature (unused in walk-forward;
        the effective holdout is ``max(horizons)``).
    max_folds : int
        Maximum number of backtest origins.
    horizons : list[int] or None
        Forecast horizons (in steps) to evaluate.  Default ``[7, 14, 30]``.
    step : int
        Sliding-origin step — number of steps between consecutive origins.

    Returns
    -------
    BacktestResult
    """
    if horizons is None:
        horizons = list(_DEFAULT_HORIZONS)
    h_max = max(horizons)
    primary_h = min(horizons)  # used for directional accuracy + flattened residuals

    y = y.dropna().copy()
    n = len(y)

    # Need at least min_train + h_max points for one fold.
    if n < min_train + h_max:
        logger.warning(
            "Backtest: series too short (%d < %d + %d), returning empty",
            n, min_train, h_max,
        )
        return BacktestResult(
            per_model_mape={},
            ensemble_mape=float("inf"),
            naive_mape=float("inf"),
            n_folds=0,
            residuals=[],
            metric="MAPE",
            per_horizon_mape={},
            residuals_by_horizon={},
            directional_accuracy={},
            per_model_mase={},
            per_model_crps={},
            interval_coverage={},
            interval_width={},
        )

    # Build the list of origins (sliding window).
    last_origin = n - h_max
    origins: list[int] = []
    origin = min_train
    while origin <= last_origin and len(origins) < max_folds:
        origins.append(origin)
        origin += step
    if not origins:
        origins.append(min_train)

    # Check if ANY actual value is zero → use RMSE instead of MAPE
    use_rmse = bool((y == 0).any())
    metric = "RMSE" if use_rmse else "MAPE"

    # Always evaluate seasonal_naive as the honesty-gate benchmark.
    all_model_names = list(models.keys())
    if "seasonal_naive" not in all_model_names:
        all_model_names = all_model_names + ["seasonal_naive"]

    # Accumulators: per-horizon, per-model.
    # errors_by_horizon[h][model] = list of APE/RMSE values across folds.
    errors_by_horizon: dict[int, dict[str, list[float]]] = {
        h: {name: [] for name in all_model_names} for h in horizons
    }
    # Per-model residuals so conformal calibration (Phase D) can use the
    # BEST model's residuals rather than the seasonal_naive benchmark
    # (which is systematically too wide).  `residuals_by_horizon` is
    # resolved to the best model after the loop.
    residuals_by_model_horizon: dict[str, dict[int, list[float]]] = {
        name: {h: [] for h in horizons} for name in all_model_names
    }
    # Directional: per-model, at primary horizon.
    dir_correct: dict[str, int] = {name: 0 for name in all_model_names}
    dir_total_per_model: dict[str, int] = {name: 0 for name in all_model_names}

    evaluated_folds = 0
    for T in origins:
        y_train = y.iloc[:T]
        y_actual = y.iloc[T:T + h_max]
        if len(y_actual) < primary_h:
            continue

        evaluated_folds += 1
        last_train_val = float(y_train.iloc[-1])

        for name in all_model_names:
            if name == "seasonal_naive" and name not in models:
                from app.services.forecasting.models.naive import SeasonalNaive
                model = SeasonalNaive(seasonal_period=seasonal_period)
            else:
                model = models[name]

            pred = _fit_forecast(model, y_train, h_max, seasonal_period)
            if pred is None:
                # Model crashed on this fold — record inf for every horizon.
                for h in horizons:
                    errors_by_horizon[h][name].append(float("inf"))
                continue

            pred_arr = np.asarray(pred, dtype=float)

            for h in horizons:
                if h > len(y_actual) or h > len(pred_arr):
                    errors_by_horizon[h][name].append(float("inf"))
                    continue
                actual_h = float(y_actual.iloc[h - 1])
                pred_h = float(pred_arr[h - 1])
                resid = actual_h - pred_h
                residuals_by_model_horizon[name][h].append(resid)
                err = _error_metric(actual_h, pred_h, use_rmse)
                errors_by_horizon[h][name].append(err)

            # Directional accuracy at primary horizon (count per model).
            if primary_h <= len(pred_arr) and primary_h <= len(y_actual):
                pred_dir = float(pred_arr[primary_h - 1]) - last_train_val
                actual_dir = float(y_actual.iloc[primary_h - 1]) - last_train_val
                dir_total_per_model[name] += 1
                if (pred_dir > 0) == (actual_dir > 0):
                    dir_correct[name] += 1

    # Aggregate per-horizon MAPE (or RMSE — sqrt applied here for RMSE path).
    per_horizon_mape: dict[int, dict[str, float]] = {}
    for h in horizons:
        per_horizon_mape[h] = {}
        for name in all_model_names:
            errs = [e for e in errors_by_horizon[h][name] if np.isfinite(e)]
            if not errs:
                per_horizon_mape[h][name] = float("inf")
            elif use_rmse:
                per_horizon_mape[h][name] = float(np.sqrt(np.mean(errs)))
            else:
                per_horizon_mape[h][name] = float(np.mean(errs))

    # Backward-compat: per_model_mape = mean across horizons (excluding inf-only).
    per_model_mape: dict[str, float] = {}
    for name in all_model_names:
        vals = [per_horizon_mape[h][name] for h in horizons
                if np.isfinite(per_horizon_mape[h][name])]
        per_model_mape[name] = float(np.mean(vals)) if vals else float("inf")

    # Ensemble MAPE: mean of (non-baseline) model MAPEs.
    ensemble_candidates = [
        v for k, v in per_model_mape.items()
        if k not in ("naive_last", "seasonal_naive", "mean_reversion")
        and v < float("inf")
    ]
    ensemble_mape = float(np.mean(ensemble_candidates)) if ensemble_candidates else float("inf")

    naive_mape = per_model_mape.get("seasonal_naive", float("inf"))

    # ── MASE (Mean Absolute Scaled Error) ──────────────────────────────
    # MASE = MAE_model / MAE_seasonal_naive. Scale-free; standard TS metric.
    sn_mae_denom = float("inf")
    if "seasonal_naive" in residuals_by_model_horizon:
        all_sn_resids = [
            r for h in horizons
            for r in residuals_by_model_horizon["seasonal_naive"][h]
        ]
        if all_sn_resids:
            sn_mae_denom = float(np.mean(np.abs(all_sn_resids)))

    per_model_mase: dict[str, float] = {}
    for name in all_model_names:
        resids = [
            r for h in horizons
            for r in residuals_by_model_horizon.get(name, {}).get(h, [])
        ]
        if not resids or sn_mae_denom == 0:
            per_model_mase[name] = float("inf")
        else:
            per_model_mase[name] = float(np.mean(np.abs(resids))) / sn_mae_denom

    # ── CRPS + interval coverage (probabilistic models only) ──────────
    per_model_crps: dict[str, float | None] = {}
    interval_coverage: dict[str, float | None] = {}
    interval_width: dict[str, float | None] = {}
    for name in all_model_names:
        if name not in models:
            per_model_crps[name] = None
            interval_coverage[name] = None
            interval_width[name] = None
            continue
        model = models[name]
        try:
            qfc = model.forecast_quantiles(h_max, quantiles=[0.1, 0.5, 0.9])
        except Exception:
            qfc = None
        if qfc is None:
            per_model_crps[name] = None
            interval_coverage[name] = None
            interval_width[name] = None
        else:
            # Compute CRPS + coverage on the last fold (full refit is expensive)
            try:
                last_T = origins[-1]
                model.fit(y.iloc[:last_T], seasonal_period=seasonal_period)
                q = model.forecast_quantiles(h_max, quantiles=[0.1, 0.5, 0.9])
                y_actual = y.iloc[last_T:last_T + h_max].values.astype(float)
                q10 = np.asarray(q[0.1][:len(y_actual)], dtype=float)
                q50 = np.asarray(q[0.5][:len(y_actual)], dtype=float)
                q90 = np.asarray(q[0.9][:len(y_actual)], dtype=float)
                per_model_crps[name] = float(
                    np.mean(np.abs(q50 - y_actual) + 0.5 * (q90 - q10))
                )
                interval_coverage[name] = float(
                    np.mean((y_actual >= q10) & (y_actual <= q90))
                )
                interval_width[name] = float(np.mean(q90 - q10))
            except Exception:
                per_model_crps[name] = None
                interval_coverage[name] = None
                interval_width[name] = None

    # Resolve residuals_by_horizon to the BEST model (lowest mean per_model_mape,
    # excluding baselines) — conformal calibration calibrates against the model
    # whose forecast is actually published (the ensemble is dominated by it).
    best_model = min(
        (n for n in per_model_mape
         if n not in ("naive_last", "seasonal_naive", "mean_reversion")
         and np.isfinite(per_model_mape[n])),
        key=lambda n: per_model_mape[n],
        default="seasonal_naive",
    )
    residuals_by_horizon: dict[int, list[float]] = {
        h: residuals_by_model_horizon[best_model].get(h, [])
        for h in horizons
    }

    # Directional accuracy (per-model denominator).
    directional_accuracy: dict[str, float] = {}
    for name in all_model_names:
        if dir_total_per_model[name] > 0:
            directional_accuracy[name] = dir_correct[name] / dir_total_per_model[name]

    # Backward-compat flattened residuals at primary horizon.
    flat_residuals = list(residuals_by_horizon.get(primary_h, []))

    return BacktestResult(
        per_model_mape=per_model_mape,
        ensemble_mape=ensemble_mape,
        naive_mape=naive_mape,
        n_folds=evaluated_folds,
        residuals=flat_residuals,
        metric=metric,
        per_horizon_mape=per_horizon_mape,
        residuals_by_horizon=residuals_by_horizon,
        directional_accuracy=directional_accuracy,
        per_model_mase=per_model_mase,
        per_model_crps=per_model_crps,
        interval_coverage=interval_coverage,
        interval_width=interval_width,
    )


# ── helpers ────────────────────────────────────────────────────────────

def _fit_forecast(
    model: ForecastModel,
    y_train: pd.Series,
    h: int,
    seasonal_period: int,
) -> np.ndarray | None:
    """Fit + forecast one model on one fold.  Returns prediction array or None."""
    try:
        model.fit(y_train, seasonal_period=seasonal_period)
        pred = model.forecast(h)
        return np.asarray(pred, dtype=float)
    except Exception as exc:
        logger.debug("Model %s failed on backtest fold: %s", getattr(model, "name", "?"), exc)
        return None


def _error_metric(actual: float, predicted: float, use_rmse: bool) -> float:
    """Single-point error (APE or squared error → RMSE later)."""
    if use_rmse:
        return float((actual - predicted) ** 2)
    if actual == 0:
        return float("inf")
    return float(abs((actual - predicted) / actual))
