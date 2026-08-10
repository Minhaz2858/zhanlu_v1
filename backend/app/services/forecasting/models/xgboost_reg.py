"""XGBoost lag-feature regression model (lazy import).

Constructs lag features + calendar features, trains an XGBoost regressor,
and produces a recursive multi-step forecast.

XGBoost is imported lazily inside ``__init__``.  If it is not installed,
construction raises :class:`ImportError`, which is caught by the model
registry's ``build_model_pool()`` and the model is silently skipped.
"""

from __future__ import annotations

import logging
import os

import numpy as np
import pandas as pd

from app.services.forecasting.models.base import ForecastModel, ModelFitError

logger = logging.getLogger(__name__)

_XGB_TUNING_ENABLED = os.getenv("FORECAST_XGB_TUNING_ENABLED", "false").lower() in ("true", "1")
_FEATURE_SELECTION_ENABLED = os.getenv("FORECAST_FEATURE_SELECTION_ENABLED", "false").lower() in ("true", "1")


class XGBoostReg(ForecastModel):
    """XGBoost regression on lag + calendar features (+ optional exogenous)."""

    name: str = "xgboost_reg"
    min_history: int = 60  # needs sufficient data for lag features
    uses_exog: bool = False  # set True when exogenous features are supplied

    def __init__(self) -> None:
        import xgboost as xgb  # lazy — ImportError caught by registry

        self._xgb = xgb
        self._model: xgb.XGBRegressor | None = None
        self._last_values: np.ndarray | None = None
        self._n_lags: int = 14
        self._seasonal_period: int = 7
        self._exog_feature_names: list[str] = []
        self._exog_last: pd.DataFrame | None = None
        self._product_key: str = ""
        self._tuned_params: dict | None = None

    # ------------------------------------------------------------------
    def fit(
        self,
        y: pd.Series,
        seasonal_period: int | None = None,
        exog: pd.DataFrame | None = None,
        **kwargs,
    ) -> None:
        self._product_key = kwargs.pop("product_key", "")
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

        values = y.values.astype(float)
        X_endo, Y = self._build_features(values, self._n_lags, self._seasonal_period)

        # Merge exogenous features if provided
        if exog is not None and len(exog) > 0:
            self.uses_exog = True
            # Align exog to y (drop rows before first usable lag)
            exog_slice = exog.loc[y.index[n_lags:]].copy()
            common_exog = exog_slice.reindex(y.index[n_lags:])
            if common_exog.isna().all().all():
                logger.warning("Exog features all NaN after alignment — ignoring")
                X = X_endo
            else:
                common_exog = common_exog.fillna(0.0)
                self._exog_feature_names = list(common_exog.columns)
                X_exog = common_exog.values.astype(float)
                min_rows = min(len(X_endo), len(X_exog))
                X = np.hstack([X_endo[:min_rows], X_exog[:min_rows]])
                Y = Y[:min_rows]
                self._exog_last = exog.iloc[-1:] if len(exog) > 0 else None
        else:
            X = X_endo

        if len(X) < 10:
            raise ModelFitError(
                f"XGBoostReg: insufficient training samples after "
                f"feature construction: {len(X)}"
            )

        # ---- Bayesian hyperparameter tuning (flag-gated) ----
        tuned_params = self._get_params()
        if _XGB_TUNING_ENABLED and self._product_key:
            try:
                from app.services.forecasting.models.xgboost_tuner import (
                    tune_xgboost_params,
                )
                tuned_params = tune_xgboost_params(
                    y=y,
                    product_key=self._product_key,
                    seasonal_period=self._seasonal_period,
                    exog=exog,
                )
                self._tuned_params = tuned_params
                logger.info(
                    "XGBoost tuning applied for %s: %s",
                    self._product_key,
                    {k: v for k, v in tuned_params.items() if k != "verbosity"},
                )
            except Exception as exc:
                logger.warning(
                    "XGBoost tuning failed for %s (using defaults): %s",
                    self._product_key, exc,
                )
                tuned_params = self._get_params()

        self._model = self._xgb.XGBRegressor(**tuned_params)
        self._model.fit(X, Y)

        # ---- Feature selection via permutation importance (flag-gated) ----
        if (
            _FEATURE_SELECTION_ENABLED
            and self._product_key
            and self._exog_feature_names
            and hasattr(self._model, "feature_importances_")
        ):
            try:
                from app.services.forecasting.features.feature_selector import (
                    select_features,
                )
                selected = select_features(
                    model=self._model,
                    X=X,
                    Y=Y,
                    feature_names=self._exog_feature_names,
                    product_key=self._product_key,
                )
                if selected and len(selected) < len(self._exog_feature_names):
                    logger.info(
                        "Feature selection for %s: %d → %d features",
                        self._product_key,
                        len(self._exog_feature_names),
                        len(selected),
                    )
                    self._exog_feature_names = selected
            except Exception as exc:
                logger.warning(
                    "Feature selection failed for %s: %s", self._product_key, exc
                )

        self._last_values = values

    # ------------------------------------------------------------------
    def _get_params(self) -> dict:
        """Return XGBoost params (defaults or tuned)."""
        return dict(
            n_estimators=100,
            max_depth=3,
            learning_rate=0.1,
            subsample=0.8,
            random_state=42,
            verbosity=0,
        )

    # ------------------------------------------------------------------
    def forecast(self, h: int, exog_future: pd.DataFrame | None = None) -> pd.Series:
        if self._model is None or self._last_values is None:
            raise ModelFitError("XGBoostReg.forecast() called before fit()")
        if h < 1:
            raise ModelFitError(f"horizon must be >= 1, got {h}")

        window = list(self._last_values[-self._n_lags:])
        predictions: list[float] = []

        for step in range(h):
            abs_step = len(self._last_values) + step
            features = self._make_single_features(
                window, abs_step, self._n_lags, self._seasonal_period
            )
            # Append exogenous features if available
            if self.uses_exog and exog_future is not None and len(exog_future) > step:
                exog_row = exog_future.iloc[step].values.astype(float)
                features = np.hstack([features, exog_row])
            elif self.uses_exog and self._exog_last is not None:
                exog_row = self._exog_last.iloc[0].values.astype(float)
                features = np.hstack([features, exog_row])

            pred = float(self._model.predict(features.reshape(1, -1))[0])
            predictions.append(pred)
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
