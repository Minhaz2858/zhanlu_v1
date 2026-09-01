"""P3-4: Purged K-Fold cross-validation with embargo gap.

Eliminates forward-looking bias for autocorrelated series by inserting an
embargo period between training and testing folds.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class PurgedCVResult:
    """Result of purged cross-validation."""

    n_folds: int
    per_fold_mape: list[float]
    mean_mape: float
    std_mape: float
    embargo: int
    leakage_score: float  # 0 = no leakage, > 0.5 = possible leakage detected
    notes: list[str]


def evaluate_purged(
    y: pd.Series,
    models: dict,
    seasonal_period: int = 7,
    n_folds: int = 5,
    embargo: int = 7,
    min_train: int = 30,
    min_test: int = 7,
    horizon: int = 7,
    metric: str = "mape",
) -> PurgedCVResult:
    """Purged K-fold cross-validation with embargo gap.

    Splits the series into n_folds with an embargo period between train and
    test to prevent leakage from autocorrelation.

    Args:
        y: Target series.
        models: Dict of {name: model_instance} with fit/forecast.
        seasonal_period: Seasonality period for models.
        n_folds: Number of folds.
        embargo: Embargo gap length (points removed between train and test).
        min_train: Minimum training window.
        min_test: Minimum test window.
        horizon: Forecast horizon per fold.
        metric: Error metric ("mape" or "rmse").

    Returns:
        PurgedCVResult with per-fold MAPE and leakage score.
    """
    y = y.dropna().sort_index()
    n = len(y)

    if n < min_train + min_test + embargo:
        return PurgedCVResult(
            n_folds=0, per_fold_mape=[], mean_mape=float("nan"),
            std_mape=float("nan"), embargo=embargo, leakage_score=0.0,
            notes=["series too short for purged CV"],
        )

    # Adjust n_folds based on available data
    max_possible = max(1, (n - min_train - embargo) // (min_test + embargo))
    n_folds = min(n_folds, max_possible)

    fold_errors: dict[str, list[float]] = {name: [] for name in models}
    leakage_scores: list[float] = []

    test_size = max(min_test, (n - min_train - embargo * (n_folds - 1)) // n_folds)

    for fold in range(n_folds):
        # Train: from start to split_point
        # Embargo: next 'embargo' points
        # Test: after embargo, length test_size
        split = min(n, min_train + fold * (test_size + embargo))
        train_end = split
        test_start = min(n, split + embargo)
        test_end = min(n, test_start + test_size)

        if test_end - test_start < min_test:
            continue

        y_train = y.iloc[:train_end].copy()
        y_test = y.iloc[test_start:test_end].copy()

        # Ensure series have freq attribute (avoids Timestamp+str error in models)
        if y_train.index.freq is None:
            y_train.index.freq = pd.infer_freq(y_train.index)
        if y_test.index.freq is None:
            y_test.index.freq = pd.infer_freq(y_test.index)

        if len(y_train) < min_train or len(y_test) < min_test:
            continue

        for name, model in models.items():
            try:
                model.fit(y_train, seasonal_period=seasonal_period)
                fc = model.forecast(min(horizon, len(y_test)))
                actual = y_test.values[:len(fc)]
                pred = fc.values[:len(fc)]

                if metric == "mape":
                    err = np.mean(np.abs((actual - pred) / np.maximum(np.abs(actual), 0.01))) * 100
                else:
                    err = np.sqrt(np.mean((actual - pred) ** 2))
                fold_errors[name].append(float(err))
            except Exception as exc:
                logger.warning("[purged-cv] fold %d model %s failed: %s", fold, name, exc)

        # Leakage check: train-test correlation (high r = possible leakage)
        if len(y_train) > 5 and len(y_test) > 5:
            k = min(10, len(y_train), len(y_test))
            train_tail = y_train.iloc[-k:]
            test_head = y_test.iloc[:k]
            if len(train_tail) == len(test_head) and len(train_tail) > 2:
                r = np.corrcoef(train_tail.values, test_head.values)[0, 1]
                if not np.isnan(r):
                    leakage_scores.append(abs(r))

    # Aggregate
    all_mape: list[float] = []
    for name_errors in fold_errors.values():
        all_mape.extend(name_errors)

    mean_mape = float(np.mean(all_mape)) if all_mape else float("nan")
    std_mape = float(np.std(all_mape)) if all_mape else float("nan")

    leakage_score = float(np.mean(leakage_scores)) if leakage_scores else 0.0

    notes: list[str] = []
    if leakage_score > 0.5:
        notes.append(f"WARNING: high leakage score {leakage_score:.2f} — consider larger embargo")
    if n_folds < 3:
        notes.append(f"Only {n_folds} folds — limited statistical power")

    return PurgedCVResult(
        n_folds=len(fold_errors.get(next(iter(models)), [])) if models else 0,
        per_fold_mape=all_mape,
        mean_mape=round(mean_mape, 4) if not np.isnan(mean_mape) else mean_mape,
        std_mape=round(std_mape, 4) if not np.isnan(std_mape) else std_mape,
        embargo=embargo,
        leakage_score=round(leakage_score, 4),
        notes=notes,
    )
