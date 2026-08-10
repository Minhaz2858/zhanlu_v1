"""
Stacking meta-learner for forecast ensemble.

Collects per-fold predictions from all base models during backtest,
trains a Ridge regression on (base_model_predictions → actual), and
blends forecasts via the meta-learner at inference time.

Flag: FORECAST_STACKING_ENABLED (default false)
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from app.services.forecasting.models.base import ForecastModel

logger = logging.getLogger(__name__)


class StackingMetaLearner:
    """Ridge regression meta-learner trained on backtest base-model predictions.

    During the backtest phase, record per-fold predictions from all base models
    and the corresponding actuals.  After backtest, :meth:`fit_meta` trains a
    Ridge regressor that learns the optimal linear combination of base models.

    At forecast time, :meth:`blend` feeds the current base forecasts through
    the meta-learner to produce the ensemble prediction.

    Parameters
    ----------
    alpha : float
        Ridge regularisation strength.  Default 1.0.
    scale : bool
        Whether to standard-scale inputs before fitting.  Default True.
    """

    def __init__(self, alpha: float = 1.0, scale: bool = True) -> None:
        self._model = Ridge(alpha=alpha, fit_intercept=True)
        self._scaler = StandardScaler() if scale else None
        self._fitted = False
        self._model_cols: list[str] = []

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def record_fold(
        self,
        model_predictions: dict[str, np.ndarray | list[float]],
        actuals: np.ndarray | list[float],
    ) -> None:
        """Record one backtest fold's predictions and actuals.

        Parameters
        ----------
        model_predictions : dict[str, array-like]
            {model_name: array of shape (n_samples,)} per model for this fold.
        actuals : array-like of shape (n_samples,)
            Ground-truth values for the fold.
        """
        if not hasattr(self, "_preds_list"):
            self._preds_list: list[pd.DataFrame] = []
            self._actuals_list: list[np.ndarray] = []

        df = pd.DataFrame(model_predictions)
        self._preds_list.append(df)
        self._actuals_list.append(np.asarray(actuals, dtype=float))

    def fit_meta(self) -> bool:
        """Fit Ridge meta-learner on all recorded folds.

        Returns
        -------
        bool
            True if fitting succeeded, False if insufficient data.
        """
        if not hasattr(self, "_preds_list") or len(self._preds_list) == 0:
            logger.warning("StackingMetaLearner: no folds recorded, skipping fit")
            return False

        X_stack = pd.concat(self._preds_list, axis=0).astype(float)
        y_stack = np.concatenate(self._actuals_list).astype(float)

        # Handle NaN: fill with 0 (neutral) — models may fail on some folds
        nan_count = X_stack.isna().sum().sum()
        if nan_count > 0:
            logger.debug("StackingMetaLearner: %d NaN in predictions, filling with 0", nan_count)
        X_stack = X_stack.fillna(0.0)
        y_nan_mask = pd.isna(y_stack)
        y_stack = y_stack[~y_nan_mask]
        X_stack = X_stack[~y_nan_mask]

        n_samples = len(y_stack)
        if n_samples < 10 or X_stack.shape[1] < 2:
            logger.warning(
                "StackingMetaLearner: insufficient data (n=%d, p=%d)",
                n_samples, X_stack.shape[1],
            )
            return False

        self._model_cols = list(X_stack.columns)

        if self._scaler is not None:
            X_scaled = self._scaler.fit_transform(X_stack)
        else:
            X_scaled = X_stack.values

        self._model.fit(X_scaled, y_stack)
        self._fitted = True

        coefs = dict(zip(self._model_cols, self._model.coef_))
        logger.info(
            "StackingMetaLearner fitted: n=%d, models=%d, intercept=%.4f, coefs=%s",
            n_samples, len(self._model_cols), self._model.intercept_, coefs,
        )
        return True

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def blend(
        self, base_forecasts: dict[str, pd.Series], h: int,
    ) -> pd.Series | None:
        """Blend base model forecasts through the meta-learner.

        Parameters
        ----------
        base_forecasts : dict[str, pd.Series]
            {model_name: forecast_series} for the current target.
        h : int
            Forecast horizon (used for index construction).

        Returns
        -------
        pd.Series | None
            Blended forecast, or None if meta-learner not fitted.
        """
        if not self._fitted:
            return None

        # Build row for each horizon step
        rows = []
        for step in range(h):
            row = {}
            for col in self._model_cols:
                if col in base_forecasts:
                    fc = base_forecasts[col]
                    row[col] = float(fc.iloc[step]) if step < len(fc) else float(fc.iloc[-1])
                else:
                    # Missing model — use mean of available models
                    row[col] = 0.0  # neutral fallback
            rows.append(row)

        X_new = pd.DataFrame(rows, columns=self._model_cols).astype(float)

        if self._scaler is not None:
            X_scaled = self._scaler.transform(X_new)
        else:
            X_scaled = X_new.values

        y_pred = self._model.predict(X_scaled)

        # Derive index from the first available base forecast
        idx = next(iter(base_forecasts.values())).index[:h]
        return pd.Series(y_pred, index=idx, name="stacking")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def fitted(self) -> bool:
        return self._fitted

    @property
    def feature_names(self) -> list[str]:
        return list(self._model_cols)
