"""Direct multi-step XGBoost — one model per horizon step.

Unlike the recursive XGBoostReg (which feeds predictions back into the input
window, causing compounding error at long horizons), this model trains a
**separate XGBoost regressor per forecast step**.  Each model predicts the
value at step t+h directly from the same base features (lags + calendar).

This eliminates the recursive error chain, greatly improving accuracy at
the 30-day horizon where recursive forecasts often blow up.

Flag-gated via ``FORECAST_XGB_DIRECT_ENABLED`` (default false).
Registered in ``build_model_pool()`` as ``xgboost_direct``.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from app.services.forecasting.models.base import ForecastModel, ModelFitError

logger = logging.getLogger(__name__)

# Commonly used horizons — one model trained per step
_DEFAULT_HORIZONS = [3, 7, 14, 30]
_MAX_HORIZON = 30


class XGBoostDirect(ForecastModel):
    """Direct multi-step XGBoost: one regressor per horizon step."""

    name: str = "xgboost_direct"
    min_history: int = 90  # needs more data (one model per step)
    uses_exog: bool = False

    def __init__(self, max_horizon: int = _MAX_HORIZON, **xgb_kwargs: Any) -> None:
        import xgboost as xgb

        self._xgb = xgb
        self._max_horizon = max_horizon
        self._default_kwargs: dict[str, Any] = dict(xgb_kwargs)
        self._models: dict[int, xgb.XGBRegressor] = {}  # step → model
        self._n_lags: int = 14
        self._seasonal_period: int = 7
        self._last_window: np.ndarray | None = None
        self._fitted: bool = False

    # ------------------------------------------------------------------
    def fit(
        self,
        y: pd.Series,
        seasonal_period: int | None = None,
        exog: pd.DataFrame | None = None,
        **kwargs: Any,
    ) -> None:
        _ = kwargs
        if seasonal_period is not None:
            self._seasonal_period = seasonal_period

        y = y.dropna()
        if len(y) < self.min_history:
            raise ModelFitError(
                f"XGBoostDirect needs ≥{self.min_history} obs, got {len(y)}"
            )

        n_lags = min(self._n_lags, len(y) - 10)
        self._n_lags = max(3, n_lags)
        values = y.values.astype(float)
        self._last_window = values[-self._n_lags :].copy()

        sp = self._seasonal_period
        base_kw: dict[str, Any] = {
            "random_state": 42,
            "verbosity": 0,
            "n_estimators": self._default_kwargs.get("n_estimators", 100),
            "max_depth": self._default_kwargs.get("max_depth", 3),
            "learning_rate": self._default_kwargs.get("learning_rate", 0.1),
            "subsample": self._default_kwargs.get("subsample", 0.8),
            "colsample_bytree": self._default_kwargs.get("colsample_bytree", 1.0),
            "reg_alpha": self._default_kwargs.get("reg_alpha", 0.0),
            "reg_lambda": self._default_kwargs.get("reg_lambda", 1.0),
        }

        self._models.clear()

        for h in range(1, self._max_horizon + 1):
            X_h, Y_h = _build_direct_features(
                values, n_lags, sp, horizon_step=h
            )
            if X_h is None or len(X_h) < max(10, h):
                logger.debug(
                    "XGBoostDirect step=%d: insufficient samples (%s), skipping",
                    h, len(X_h) if X_h is not None else 0,
                )
                continue

            model = self._xgb.XGBRegressor(**base_kw)
            try:
                model.fit(X_h, Y_h)
            except Exception as exc:
                logger.warning(
                    "XGBoostDirect step=%d fit failed: %s", h, exc
                )
                continue

            self._models[h] = model

        if len(self._models) == 0:
            raise ModelFitError("XGBoostDirect: all horizon steps failed to train")

        self._fitted = True
        logger.info(
            "XGBoostDirect trained %d horizon-step models (1→%d)",
            len(self._models), max(self._models.keys()),
        )

    # ------------------------------------------------------------------
    def forecast(self, h: int, exog_future: pd.DataFrame | None = None) -> pd.Series:
        _ = exog_future
        if not self._fitted or self._last_window is None:
            raise ModelFitError("XGBoostDirect.forecast() called before fit()")

        h_usable = min(h, self._max_horizon)
        predictions: list[float] = []

        for step in range(1, h_usable + 1):
            if step in self._models:
                feats = np.array(
                    _make_direct_features(
                        self._last_window.tolist(),
                        len(self._last_window) + step,
                        self._n_lags,
                        self._seasonal_period,
                    )
                )
                pred = float(self._models[step].predict(feats.reshape(1, -1))[0])
                predictions.append(pred)
            else:
                # Fall back to last value for horizon steps without a model
                predictions.append(float(self._last_window[-1]))

        # Pad if h > max_horizon (naive forward-fill)
        while len(predictions) < h:
            predictions.append(predictions[-1] if predictions else float(self._last_window[-1]))

        return pd.Series(predictions, name=self.name)


# ---------------------------------------------------------------------------
# Feature builders (mirror xgboost_reg pattern but train with h-step-ahead targets)
# ---------------------------------------------------------------------------

def _build_direct_features(
    values: np.ndarray,
    n_lags: int,
    seasonal_period: int,
    horizon_step: int,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Build (X, Y) for direct h-step-ahead prediction.

    X = lag+calendar features from window [i-n_lags : i].
    Y = value at i + horizon_step (the target h steps ahead).
    """
    n = len(values)
    if n <= n_lags + horizon_step:
        return None, None

    rows: list[list[float]] = []
    targets: list[float] = []

    for i in range(n_lags, n - horizon_step):
        window = values[i - n_lags : i].tolist()
        feats = _make_direct_features(window, i, n_lags, seasonal_period)
        rows.append(feats)
        targets.append(float(values[i + horizon_step]))

    return np.array(rows), np.array(targets)


def _make_direct_features(
    window: list[float],
    abs_step: int,
    n_lags: int,
    seasonal_period: int,
) -> list[float]:
    """Build flat feature vector (same shape as xgboost_reg)."""
    feats: list[float] = []

    # Lag features (lag 1 .. n_lags)
    for lag in range(1, n_lags + 1):
        feats.append(window[-lag] if lag <= len(window) else window[0])

    # Seasonal lag
    s_lag = min(seasonal_period, len(window))
    feats.append(window[-s_lag])

    # Day-of-week cyclic features
    dow = abs_step % seasonal_period
    feats.append(np.sin(2 * np.pi * dow / seasonal_period))
    feats.append(np.cos(2 * np.pi * dow / seasonal_period))

    # Rolling statistics (7-day)
    k = min(7, len(window))
    feats.append(float(np.mean(window[-k:])))
    feats.append(float(np.std(window[-k:]) + 1e-10))

    return feats
