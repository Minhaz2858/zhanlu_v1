"""ARIMA model with manual grid search (no pmdarima dependency).

Uses statsmodels ``ARIMA`` (which wraps SARIMAX) with a small grid of
(p, d, q) orders.  Picks the configuration with the lowest AIC.
"""

from __future__ import annotations

import logging
import warnings

import pandas as pd
from statsmodels.tsa.arima.model import ARIMA

from app.services.forecasting.models.base import ForecastModel, ModelFitError

logger = logging.getLogger(__name__)

# Compact grid — business series rarely need p,q > 2
_ARIMA_GRID = [
    (p, d, q)
    for p in (0, 1, 2)
    for d in (0, 1)
    for q in (0, 1, 2)
]


class ARIMAModel(ForecastModel):
    """ARIMA model with AIC-based order selection."""

    name: str = "arima"
    min_history: int = 14

    def __init__(self) -> None:
        self._fitted_result = None
        self._best_order: tuple[int, int, int] | None = None

    # ------------------------------------------------------------------
    def fit(
        self,
        y: pd.Series,
        seasonal_period: int | None = None,
        **kwargs,
    ) -> None:
        _ = kwargs
        _ = seasonal_period
        y = y.dropna()
        if len(y) < self.min_history:
            raise ModelFitError(
                f"ARIMA requires at least {self.min_history} non-null "
                f"observations, got {len(y)}"
            )

        best_aic: float = float("inf")
        best_result = None
        best_order: tuple[int, int, int] | None = None

        for order in _ARIMA_GRID:
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=UserWarning)
                    warnings.filterwarnings("ignore", category=FutureWarning)
                    model = ARIMA(y.values, order=order)
                    result = model.fit()
                if result.aic < best_aic:
                    best_aic = result.aic
                    best_result = result
                    best_order = order
            except (ValueError, RuntimeError, Exception) as exc:
                logger.debug("ARIMA order=%s failed: %s", order, exc)
                continue

        if best_result is None or best_order is None:
            raise ModelFitError(
                "ARIMA failed to converge for any order in the grid"
            )

        self._fitted_result = best_result
        self._best_order = best_order
        logger.debug("ARIMA best order=%s aic=%.2f", best_order, best_aic)

    # ------------------------------------------------------------------
    def forecast(self, h: int) -> pd.Series:
        if self._fitted_result is None:
            raise ModelFitError("ARIMA.forecast() called before fit()")
        if h < 1:
            raise ModelFitError(f"horizon must be >= 1, got {h}")

        pred = self._fitted_result.forecast(h)
        return pd.Series(pred, name=self.name)
