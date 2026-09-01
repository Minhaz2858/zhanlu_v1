"""Mean-reversion sanity baseline.

Forecast = historical mean, repeated *h* times.  This is a deliberately
weak baseline; if it ever beats the ensemble, the data is effectively
unforecastable.
"""

from __future__ import annotations

import pandas as pd

from app.services.forecasting.models.base import ForecastModel, ModelFitError


class MeanReversion(ForecastModel):
    """Flat forecast at the historical mean (sanity baseline)."""

    name: str = "mean_reversion"
    min_history: int = 10

    def __init__(self) -> None:
        self._mean: float | None = None

    # ------------------------------------------------------------------
    def fit(
        self,
        y: pd.Series,
        seasonal_period: int | None = None,
        **kwargs,
    ) -> None:
        _ = seasonal_period
        _ = kwargs
        y = y.dropna()
        if len(y) < self.min_history:
            raise ModelFitError(
                f"MeanReversion requires at least {self.min_history} "
                f"non-null observations, got {len(y)}"
            )
        self._mean = float(y.mean())

    # ------------------------------------------------------------------
    def forecast(self, h: int) -> pd.Series:
        if self._mean is None:
            raise ModelFitError(
                "MeanReversion.forecast() called before fit()"
            )
        if h < 1:
            raise ModelFitError(f"horizon must be >= 1, got {h}")

        return pd.Series([self._mean] * h, name=self.name)
