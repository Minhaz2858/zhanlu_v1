"""XGBoost lag-feature regression model (lazy import).

Constructs lag features + calendar features, trains an XGBoost regressor,
and produces a recursive multi-step forecast.

XGBoost is imported lazily inside ``__init__``.  If it is not installed,
construction raises :class:`ImportError`, which is caught by the model
registry's ``build_model_pool()`` and the model is silently skipped.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.services.forecasting.models.base import ForecastModel, ModelFitError

logger = logging.getLogger(__name__)


class XGBoostReg(ForecastModel):
    """XGBoost regression on lag + calendar features."""

    name: str = "xgboost_reg"
    min_history: int = 60  # needs sufficient data for lag features

    def __init__(self) -> None:
        import xgboost as xgb  # lazy — ImportError caught by registry

        self._xgb = xgb
        self._model: xgb.XGBRegressor | None = None
        self._last_values: np.ndarray | None = None
        self._n_lags: int = 14
        self._seasonal_period: int = 7

    # ------------------------------------------------------------------
    def fit(
        self,
        y: pd.Series,
        seasonal_period: int | None = None,
        **kwargs,
    ) -> None:
        _ = kwargs
        if seasonal_period is not None:
            self._seasonal_period = seasonal_period

        y = y.dropna()
        if len(y) < self.min_history:
            raise ModelFitError(
                f"XGBoostReg requires at least {self.min_history} non-null "
                f"observations, got {len(y)}"
            )

        n_lags = min(self._n_lags, len(y) - 10)
        self._n_lags = max(3, n_lags)

        # Build feature matrix: lags + seasonal lag + day-of-week
        values = y.values.astype(float)
        X, Y = self._build_features(values, self._n_lags, self._seasonal_period)

        if len(X) < 10:
            raise ModelFitError(
                f"XGBoostReg: insufficient training samples after "
                f"feature construction: {len(X)}"
            )

        self._model = self._xgb.XGBRegressor(
            n_estimators=100,
            max_depth=3,
            learning_rate=0.1,
            subsample=0.8,
            random_state=42,
            verbosity=0,
        )
        self._model.fit(X, Y)
        self._last_values = values

    # ------------------------------------------------------------------
    def forecast(self, h: int) -> pd.Series:
        if self._model is None or self._last_values is None:
            raise ModelFitError("XGBoostReg.forecast() called before fit()")
        if h < 1:
            raise ModelFitError(f"horizon must be >= 1, got {h}")

        # Start with a sliding window of last known values
        window = list(self._last_values[-self._n_lags:])
        predictions: list[float] = []

        for step in range(h):
            # Absolute position for calendar features
            abs_step = len(self._last_values) + step
            features = self._make_single_features(
                window, abs_step, self._n_lags, self._seasonal_period
            )
            pred = float(self._model.predict(features.reshape(1, -1))[0])
            predictions.append(pred)
            # Slide window: drop oldest, append prediction
            window.pop(0)
            window.append(pred)

        return pd.Series(predictions, name=self.name)

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _build_features(
        values: np.ndarray,
        n_lags: int,
        seasonal_period: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        rows = []
        targets = []
        for i in range(n_lags, len(values)):
            window = values[i - n_lags : i]
            feats = XGBoostReg._make_single_features(
                window.tolist(), i, n_lags, seasonal_period
            )
            rows.append(feats)
            targets.append(values[i])
        return np.array(rows), np.array(targets)

    @staticmethod
    def _make_single_features(
        window: list[float],
        abs_step: int,
        n_lags: int,
        seasonal_period: int,
    ) -> np.ndarray:
        """Build a flat feature vector from a window of past values."""
        feats: list[float] = []

        # Lag features
        for lag in range(1, n_lags + 1):
            feats.append(window[-lag] if lag <= len(window) else window[0])

        # Seasonal lag
        s_lag = min(seasonal_period, len(window))
        feats.append(window[-s_lag])

        # Day-of-week (cyclic encoding)
        dow = abs_step % seasonal_period
        feats.append(np.sin(2 * np.pi * dow / seasonal_period))
        feats.append(np.cos(2 * np.pi * dow / seasonal_period))

        # Rolling statistics
        feats.append(float(np.mean(window[-min(7, len(window)) :])))
        feats.append(float(np.std(window[-min(7, len(window)) :]) + 1e-10))

        return np.array(feats, dtype=float)
