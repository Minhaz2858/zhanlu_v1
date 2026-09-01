"""Permutation-importance feature selection for XGBoost exogenous features.

Caches selected feature lists per product_key in /tmp/xgb_features/
so the expensive permutation loop runs only once per product.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_CACHE_DIR = "/tmp/xgb_features"
_PERMUTATION_REPEATS = 5
_MIN_RETAIN = 3  # always keep at least this many exog features
_DROP_THRESHOLD = 0.01  # drop features with importance below this


def select_features(
    model,
    X: np.ndarray,
    Y: np.ndarray,
    feature_names: list[str],
    product_key: str,
    n_retain: int = 10,
    threshold: float = 0.01,
) -> list[str]:
    """Select exogenous features via permutation importance.

    Shuffles each feature column independently, re-predicts, and measures
    the MAPE (or RMSE if values are near-zero) increase.  Low-importance
    features are dropped.

    Results are cached per product_key so the expensive loop runs once.

    Args:
        model: A fitted XGBRegressor with a ``predict()`` method.
        X: Training feature matrix.
        Y: Training targets.
        feature_names: Names of exogenous feature columns in X.
        product_key: Product identifier for caching.
        n_retain: Maximum number of features to keep.
        threshold: Drop features whose relative importance is below this.

    Returns:
        List of selected feature names (subset of feature_names).
    """
    if not feature_names or len(feature_names) <= _MIN_RETAIN:
        return list(feature_names)

    # --- check cache ---
    cache_path = _ensure_cache_dir() / f"{product_key}.json"
    cached = _read_cache(cache_path)
    if cached is not None:
        valid = [f for f in cached if f in feature_names]
        if valid:
            logger.debug("Feature selector cache hit for %s: %s", product_key, valid)
            return valid

    # --- compute baseline error ---
    try:
        Y_pred_baseline = model.predict(X)
    except Exception as exc:
        logger.warning("Feature selector: baseline predict failed: %s", exc)
        return list(feature_names)

    baseline_mape = _compute_mape(Y, Y_pred_baseline)
    if baseline_mape < 1e-10:
        baseline_mape = float(np.sqrt(np.mean((Y - Y_pred_baseline) ** 2)))

    # --- identify which columns map to each feature name ---
    # feature_names may be shorter than X.shape[1] when endo features precede exog.
    n_endo = X.shape[1] - len(feature_names)
    if n_endo < 0:
        logger.warning("Feature selector: X has fewer columns than feature_names — skipping")
        return list(feature_names)

    # --- permutation importance ---
    importances: dict[str, float] = {}
    rng = np.random.default_rng(42)

    for idx, name in enumerate(feature_names):
        col_idx = n_endo + idx
        mape_increases: list[float] = []
        for _ in range(_PERMUTATION_REPEATS):
            X_perm = X.copy()
            rng.shuffle(X_perm[:, col_idx])
            try:
                Y_perm = model.predict(X_perm)
            except Exception:
                continue
            perm_mape = _compute_mape(Y, Y_perm)
            if perm_mape < 1e-10:
                perm_mape = float(np.sqrt(np.mean((Y - Y_perm) ** 2)))
            mape_increases.append(max(0.0, perm_mape - baseline_mape))
        if mape_increases:
            importances[name] = float(np.mean(mape_increases))

    if not importances:
        return list(feature_names)

    # --- normalize to 0-1 ---
    max_imp = max(importances.values())
    if max_imp > 0:
        for name in importances:
            importances[name] = importances[name] / max_imp

    # --- select ---
    ranked = sorted(importances.items(), key=lambda x: x[1], reverse=True)
    selected = [name for name, imp in ranked if imp >= threshold]
    selected = selected[:n_retain]

    if len(selected) < _MIN_RETAIN:
        selected = [name for name, _ in ranked[:_MIN_RETAIN]]

    # --- write cache ---
    _write_cache(cache_path, selected)

    elapsed = time.time()
    logger.info(
        "Feature selector for %s: selected %d/%d features: %s",
        product_key,
        len(selected),
        len(feature_names),
        selected,
    )
    return selected


def _compute_mape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Mean absolute percentage error, avoiding div-by-zero."""
    denom = np.maximum(np.abs(y_true), 1e-10)
    return float(np.mean(np.abs(y_true - y_pred) / denom))


def _ensure_cache_dir() -> Path:
    p = Path(_CACHE_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read_cache(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return None


def _write_cache(path: Path, features: list[str]) -> None:
    try:
        path.write_text(json.dumps(features))
    except Exception as exc:
        logger.debug("Feature selector: cache write failed: %s", exc)
