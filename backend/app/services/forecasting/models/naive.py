"""NaiveLast and SeasonalNaive baseline models.

NaiveLast repeats the last observed value — the simplest possible
forecast.  SeasonalNaive repeats the value from *seasonal_period*
steps ago — the industry-standard seasonal baseline that every
more sophisticated model must beat to be useful.
"""

from __future__ import annotations

import pandas as pd

from app.services.forecasting.models.base import ForecastModel, ModelFitError


class NaiveLast(ForecastModel):
    """Forecast = last observed value, repeated *h* times."""

    name: str = "naive_last"
    min_history: int = 2

    def __init__(self) -> None:
        self._last_value: float | None = None
        self._last_index: pd.Timestamp | None = None
        self._freq: pd.DateOffset | None = None

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
                f"NaiveLast requires at least {self.min_history} "
                f"non-null observations, got {len(y)}"
            )
        self._last_value = float(y.iloc[-1])
        self._last_index = y.index[-1]
        # Infer frequency for generating future index
        try:
            if len(y.index) >= 2:
                self._freq = pd.infer_freq(y.index)
            else:
                self._freq = None
        except (TypeError, ValueError, AttributeError):
            self._freq = None

    # ------------------------------------------------------------------
    def forecast(self, h: int) -> pd.Series:
        if self._last_value is None:
            raise ModelFitError("NaiveLast.forecast() called before fit()")
        if h < 1:
            raise ModelFitError(f"horizon must be >= 1, got {h}")

        if self._freq is not None and self._last_index is not None:
            future_index = pd.date_range(
                start=self._last_index + self._freq,
                periods=h,
                freq=self._freq,
            )
        else:
            future_index = range(1, h + 1)

        return pd.Series(
            [self._last_value] * h,
            index=future_index,
            name=self.name,
        )


class SeasonalNaive(ForecastModel):
    """Forecast(t) = observed(t − seasonal_period).

    This is THE benchmark.  If an ensemble cannot beat seasonal_naive
    on backtest, the honesty gate sets ``below_naive_baseline=true``.
    """

    name: str = "seasonal_naive"
    min_history: int = 14  # minimum for daily data with period=7

    def __init__(self, seasonal_period: int = 7) -> None:
        self._seasonal_period = seasonal_period
        self._y: pd.Series | None = None

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
        if len(y) < self._seasonal_period:
            raise ModelFitError(
                f"SeasonalNaive requires at least {self._seasonal_period} "
                f"non-null observations, got {len(y)}"
            )
        self._y = y.copy()

    # ------------------------------------------------------------------
    def forecast(self, h: int) -> pd.Series:
        if self._y is None:
            raise ModelFitError("SeasonalNaive.forecast() called before fit()")
        if h < 1:
            raise ModelFitError(f"horizon must be >= 1, got {h}")

        n = len(self._y)
        period = self._seasonal_period
        values: list[float] = []
        for i in range(h):
            # map future step back to the most recent matching seasonal position
            ref_idx = n - period + (i % period)
            if ref_idx < 0:
                ref_idx += period
            values.append(float(self._y.iloc[ref_idx]))

        # Build future index
        try:
            if len(self._y.index) >= 2:
                freq = pd.infer_freq(self._y.index)
            else:
                freq = None
        except (TypeError, ValueError, AttributeError):
            freq = None
        if freq is not None:
            future_index = pd.date_range(
                start=self._y.index[-1] + freq,
                periods=h,
                freq=freq,
            )
        else:
            future_index = range(1, h + 1)

        return pd.Series(values, index=future_index, name=self.name)
