"""Expanding-window backtest evaluation.

For each model in the pool, trains on an expanding window of the
series, forecasts the holdout, and computes MAPE (or RMSE when
actuals contain zeros).  Returns per-model MAPE, ensemble MAPE,
and the naive baseline MAPE used by the guard module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.services.forecasting.models.base import ForecastModel

logger = logging.getLogger(__name__)

# Minimum number of observations required in the first training fold.
_MIN_TRAIN_SIZE = 14
# Minimum number of holdout steps per fold.
_MIN_HOLDOUT_SIZE = 3
# Maximum backtest folds (cap to bound runtime on long series).
_MAX_FOLDS = 10


@dataclass
class BacktestResult:
    """Result of expanding-window evaluation for one series."""

    per_model_mape: dict[str, float]  # model_name → MAPE (or RMSE fallback)
    ensemble_mape: float
    naive_mape: float  # seasonal_naive MAPE (the honesty gate benchmark)
    n_folds: int
    residuals: list[float]  # all holdout residuals (actual - predicted)
    metric: str = "MAPE"  # "MAPE" or "RMSE" (RMSE used when actuals contain zeros)


def evaluate(
    y: pd.Series,
    models: dict[str, ForecastModel],
    seasonal_period: int = 7,
    min_train: int = _MIN_TRAIN_SIZE,
    min_holdout: int = _MIN_HOLDOUT_SIZE,
    max_folds: int = _MAX_FOLDS,
) -> BacktestResult:
    """Evaluate all models on a single series via expanding-window holdout.

    Parameters
    ----------
    y : pd.Series
        Full time series (may contain NaN — dropped before use).
    models : dict[str, ForecastModel]
        Model pool to evaluate.  Each model's ``fit()`` / ``forecast()``
        is wrapped in try/except; crashing models are skipped.
    seasonal_period : int
        Passed to models that need a seasonal period.
    min_train : int
        Minimum observations in the first training split.
    min_holdout : int
        Minimum holdout steps per fold.
    max_folds : int
        Maximum number of backtest folds.

    Returns
    -------
    BacktestResult
    """
    y = y.dropna().copy()
    n = len(y)

    if n < min_train + min_holdout:
        logger.warning(
            "Backtest: series too short (%d < %d + %d), returning zeros",
            n, min_train, min_holdout,
        )
        return BacktestResult(
            per_model_mape={},
            ensemble_mape=float("inf"),
            naive_mape=float("inf"),
            n_folds=0,
            residuals=[],
            metric="MAPE",
        )

    # Determine fold sizes
    total_holdout = n - min_train
    if total_holdout < min_holdout:
        return BacktestResult(
            per_model_mape={},
            ensemble_mape=float("inf"),
            naive_mape=float("inf"),
            n_folds=0,
            residuals=[],
            metric="MAPE",
        )

    # Evenly distribute holdout across folds
    n_folds = min(max_folds, total_holdout // min_holdout)
    n_folds = max(1, n_folds)
    holdout_per_fold = total_holdout // n_folds

    # Check if ANY actual value is zero → use RMSE instead of MAPE
    use_rmse = float((y == 0).any())

    # Accumulators
    model_errors: dict[str, list[float]] = {
        name: [] for name in models
    }
    model_errors.setdefault("seasonal_naive", [])
    all_residuals: list[float] = []

    for fold in range(n_folds):
        train_end = min_train + fold * holdout_per_fold
        test_end = min(train_end + holdout_per_fold, n)
        if test_end - train_end < min_holdout:
            break

        y_train = y.iloc[:train_end]
        y_test = y.iloc[train_end:test_end]
        h = len(y_test)

        for name, model in models.items():
            error = _eval_one_model(
                model, y_train, y_test, h, seasonal_period, use_rmse
            )
            if error is not None:
                model_errors.setdefault(name, []).append(error)

        # Also evaluate seasonal_naive separately (even if not in the passed pool)
        if "seasonal_naive" not in models:
            from app.services.forecasting.models.naive import SeasonalNaive
            sn = SeasonalNaive(seasonal_period=seasonal_period)
            naive_err = _eval_one_model(
                sn, y_train, y_test, h, seasonal_period, use_rmse
            )
            if naive_err is not None:
                model_errors["seasonal_naive"].append(naive_err)

    # Aggregate: mean error per model across folds
    per_model_mape: dict[str, float] = {}
    for name, errors in model_errors.items():
        if errors:
            per_model_mape[name] = float(np.mean(errors))
        else:
            per_model_mape[name] = float("inf")

    # Ensemble MAPE: average of (non-naive) model MAPEs
    ensemble_candidates = [
        v for k, v in per_model_mape.items()
        if k not in ("naive_last", "seasonal_naive", "mean_reversion")
        and v < float("inf")
    ]
    if ensemble_candidates:
        ensemble_mape = float(np.mean(ensemble_candidates))
    else:
        ensemble_mape = float("inf")

    naive_mape = per_model_mape.get("seasonal_naive", float("inf"))

    metric = "RMSE" if use_rmse else "MAPE"

    return BacktestResult(
        per_model_mape=per_model_mape,
        ensemble_mape=ensemble_mape,
        naive_mape=naive_mape,
        n_folds=n_folds,
        residuals=all_residuals,
        metric=metric,
    )


# ── helpers ────────────────────────────────────────────────────────────

def _eval_one_model(
    model: ForecastModel,
    y_train: pd.Series,
    y_test: pd.Series,
    h: int,
    seasonal_period: int,
    use_rmse: bool,
) -> float | None:
    """Fit + forecast one model on one fold.  Returns error or None on failure."""
    try:
        model.fit(y_train, seasonal_period=seasonal_period)
        pred = model.forecast(h)
    except Exception as exc:
        logger.debug("Model %s failed on backtest fold: %s", model.name, exc)
        return None

    actual = y_test.values
    predicted = pred.values[:len(actual)]

    if len(predicted) < h:
        # Pad short predictions with last value
        padded = np.full(h, predicted[-1] if len(predicted) > 0 else np.nan)
        padded[:len(predicted)] = predicted
        predicted = padded

    # Align lengths
    min_len = min(len(actual), len(predicted))
    actual = actual[:min_len]
    predicted = predicted[:min_len]

    if min_len == 0:
        return None

    mask = ~np.isnan(predicted)
    if mask.sum() == 0:
        return None

    actual_masked = actual[mask]
    predicted_masked = predicted[mask]

    if use_rmse:
        mse = float(np.mean((actual_masked - predicted_masked) ** 2))
        return float(np.sqrt(mse))
    else:
        # MAPE — guard against division by zero
        with np.errstate(divide="ignore", invalid="ignore"):
            ape = np.abs((actual_masked - predicted_masked) / actual_masked)
            ape = ape[np.isfinite(ape)]
        if len(ape) == 0:
            return float("inf")
        return float(np.mean(ape))
