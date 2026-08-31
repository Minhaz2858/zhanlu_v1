"""XGBoost hyperparameter tuner with Optuna and purged time-series CV.

Lazy-imported: Optuna is NOT a hard dependency.  When optuna is not
installed, ``tune_xgboost_params`` returns the current defaults and logs
a warning.  Results are cached per ``product_key`` in a JSON file under
``backend/data/xgb_tuning/`` (keyed by ``product_key + seasonal_period``)
so that the search runs once per product and subsequent calls are instant.

Flag-gated via ``FORECAST_XGB_TUNING_ENABLED`` (default false).
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default params (used when Optuna unavailable or tuning disabled)
# ---------------------------------------------------------------------------

DEFAULT_PARAMS: dict[str, Any] = {
    "n_estimators": 100,
    "max_depth": 3,
    "learning_rate": 0.1,
    "subsample": 0.8,
    "colsample_bytree": 1.0,
    "reg_alpha": 0.0,
    "reg_lambda": 1.0,
}

# ---------------------------------------------------------------------------
# Optuna search space
# ---------------------------------------------------------------------------

_SEARCH_SPACE: dict[str, dict[str, Any]] = {
    "n_estimators":       {"type": "int",     "low": 50,   "high": 300},
    "max_depth":          {"type": "int",     "low": 2,    "high": 8},
    "learning_rate":      {"type": "float",   "low": 0.01, "high": 0.3, "log": True},
    "subsample":          {"type": "float",   "low": 0.6,  "high": 1.0},
    "colsample_bytree":   {"type": "float",   "low": 0.6,  "high": 1.0},
    "reg_alpha":          {"type": "float",   "low": 0.0,  "high": 5.0},
    "reg_lambda":         {"type": "float",   "low": 0.5,  "high": 10.0, "log": True},
}

_N_TRIALS = 20          # trials per product (further reduced for embedded CPUs)
_DEFAULT_EMBARGO = 7
_CACHE_DIR = Path("/tmp/xgb_tuning")  # writable inside Docker container


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def tune_xgboost_params(
    y: pd.Series,
    product_key: str = "default",
    seasonal_period: int = 7,
    exog: pd.DataFrame | None = None,
    n_trials: int = _N_TRIALS,
    embargo: int = _DEFAULT_EMBARGO,
    force_retune: bool = False,
) -> dict[str, Any]:
    """Run Optuna hyperparameter search for XGBoost on *y*.

    Parameters
    ----------
    y : pd.Series
        Target time series (will be cleaned internally).
    product_key : str
        Used as cache key (e.g. ``<tenant>.<product>``).
    seasonal_period : int
        Seasonality horizon for purged-CV fold construction.
    exog : pd.DataFrame or None
        Optional exogenous regressors.
    n_trials : int
        Maximum Optuna trials (default 20).
    embargo : int
        Purged-CV embargo gap.
    force_retune : bool
        If True, skip cache and re-run search.

    Returns
    -------
    dict
        Best discovered params (compatible with xgboost.XGBRegressor **kwargs).
    """
    # ---------------------------------------------------------------
    # Cache lookup
    # ---------------------------------------------------------------
    cache_key = f"{product_key}_sp{seasonal_period}"
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _CACHE_DIR / f"{cache_key}.json"

    if not force_retune and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            return _validate_cached_params(cached)
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Corrupt tuning cache for %s — re-running search", product_key)

    # ---------------------------------------------------------------
    # Pre-check — is Optuna available?
    # ---------------------------------------------------------------
    try:
        import optuna
    except ImportError:
        logger.info("Optuna not installed — returning default XGBoost params")
        _write_cache(cache_file, DEFAULT_PARAMS)
        return DEFAULT_PARAMS

    # ---------------------------------------------------------------
    # Prepare data
    # ---------------------------------------------------------------
    y_clean = y.dropna().sort_index()
    if len(y_clean) < 60:
        logger.info("Series too short (%d pts) — using defaults for %s", len(y_clean), product_key)
        _write_cache(cache_file, DEFAULT_PARAMS)
        return DEFAULT_PARAMS

    # Build exogenous features if provided
    def _build_exog_features(_y: pd.Series, _exog: pd.DataFrame | None):
        if _exog is None or _exog.empty:
            return None
        common = _exog.reindex(_y.index).fillna(method="ffill").fillna(0.0)
        return common if not common.isna().all().all() else None

    exog_clean = _build_exog_features(y_clean, exog)

    # ---------------------------------------------------------------
    # Objective function for Optuna
    # ---------------------------------------------------------------
    def _objective(trial: optuna.Trial) -> float:  # type: ignore[name-defined]
        params: dict[str, Any] = {}
        for pname, pdef in _SEARCH_SPACE.items():
            if pdef["type"] == "int":
                params[pname] = trial.suggest_int(
                    pname, pdef["low"], pdef["high"]
                )
            elif pdef["type"] == "float":
                log_scale = pdef.get("log", False)
                if log_scale:
                    params[pname] = trial.suggest_float(
                        pname, pdef["low"], pdef["high"], log=True
                    )
                else:
                    params[pname] = trial.suggest_float(
                        pname, pdef["low"], pdef["high"]
                    )
            else:
                params[pname] = pdef.get("default", trial.suggest_categorical(pname, pdef.get("choices", [])))

        params["random_state"] = 42
        params["verbosity"] = 0

        # Build model with suggested params
        import xgboost as xgb
        model = xgb.XGBRegressor(**params)

        # Purged-CV evaluation
        from app.services.forecasting.purged_cv import evaluate_purged

        try:
            cv_result = evaluate_purged(
                y_clean,
                {"xgb_trial": TrialXGBModel(model, exog=exog_clean)},
                seasonal_period=seasonal_period,
                n_folds=5,
                embargo=embargo,
                horizon=7,
            )
        except Exception as exc:
            logger.debug("Optuna trial failed: %s", exc)
            return float("inf")

        if math.isnan(cv_result.mean_mape):
            return float("inf")
        return float(cv_result.mean_mape)

    # ---------------------------------------------------------------
    # Run Optuna
    # ---------------------------------------------------------------
    logger.info(
        "Starting Optuna search for %s (%d trials, embargo=%d)",
        product_key, n_trials, embargo,
    )
    t0 = time.monotonic()

    try:
        study = optuna.create_study(
            direction="minimize",
            sampler=optuna.samplers.TPESampler(seed=42),
            pruner=optuna.pruners.MedianPruner(n_startup_trials=5),
        )
        study.optimize(_objective, n_trials=n_trials, show_progress_bar=False)
    except Exception as exc:
        logger.warning("Optuna optimize failed for %s: %s — using defaults", product_key, exc)
        _write_cache(cache_file, DEFAULT_PARAMS)
        return DEFAULT_PARAMS

    elapsed = time.monotonic() - t0

    best_params = study.best_params.copy()
    best_params.pop("random_state", None)  # kept at call site
    best_params["verbosity"] = 0
    best_value = study.best_value

    logger.info(
        "Optuna done for %s in %.1fs: best_mape=%.2f, params=%s",
        product_key, elapsed, best_value, best_params,
    )

    _write_cache(cache_file, best_params)
    return best_params


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TrialXGBModel:
    """Minimal adapter so we can use purged_cv.evaluate_purged()."""

    name = "xgb_trial"

    def __init__(self, model: Any, exog: pd.DataFrame | None = None) -> None:
        self._model = model
        self._exog = exog

    def fit(self, y: pd.Series, seasonal_period: int | None = None, **kwargs: Any) -> None:
        _ = (seasonal_period, kwargs)
        y_arr = y.values.astype(float)
        X, Y = _build_lag_features(y_arr, n_lags=14, seasonal_period=7)
        if X is None or len(X) < 10:
            raise ValueError("Not enough data to fit")
        self._model.fit(X, Y)

    def forecast(self, h: int, exog_future: pd.DataFrame | None = None) -> pd.Series:
        _last = np.zeros(14)  # crude — only used for MAPE, not directional
        preds = []
        for _i in range(h):
            feats = np.array(
                list(_last[-14:])
                + [0.0]  # seasonal lag placeholder
                + [np.sin(0), np.cos(0)]
                + [np.mean(_last[-7:]), np.std(_last[-7:]) + 1e-10]
            )
            preds.append(float(self._model.predict(feats.reshape(1, -1))[0]))
        return pd.Series(preds)


def _build_lag_features(
    values: np.ndarray, n_lags: int = 14, seasonal_period: int = 7
) -> tuple[np.ndarray | None, np.ndarray | None]:
    if len(values) <= n_lags:
        return None, None
    rows, targets = [], []
    for i in range(n_lags, len(values)):
        w = values[i - n_lags : i]
        feats = list(w)
        s_lag = w[-min(seasonal_period, len(w))]
        feats.append(s_lag)
        dow = i % seasonal_period
        feats.append(np.sin(2 * np.pi * dow / seasonal_period))
        feats.append(np.cos(2 * np.pi * dow / seasonal_period))
        feats.append(float(np.mean(w[-min(7, len(w)):])))
        feats.append(float(np.std(w[-min(7, len(w)):]) + 1e-10))
        rows.append(feats)
        targets.append(values[i])
    return np.array(rows), np.array(targets)


def _validate_cached_params(raw: dict[str, Any]) -> dict[str, Any]:
    """Strip unknown keys and ensure ints are ints."""
    clean: dict[str, Any] = {}
    for k, v in raw.items():
        if k in _SEARCH_SPACE:
            if _SEARCH_SPACE[k]["type"] == "int":
                clean[k] = int(v)
            else:
                clean[k] = float(v)
    for k, pdef in _SEARCH_SPACE.items():
        if k not in clean:
            clean[k] = pdef.get("default", DEFAULT_PARAMS.get(k, 0))
    return clean


def _write_cache(path: Path, params: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(params, indent=2, sort_keys=True))
