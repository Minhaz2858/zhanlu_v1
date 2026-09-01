"""STL decomposition + extrapolation model.

Decomposes the series into trend + seasonal + residual using statsmodels
MSTL (or STL for older versions), extrapolates the trend linearly, and
re-applies the seasonal pattern.
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd

from app.services.forecasting.models.base import ForecastModel, ModelFitError

logger = logging.getLogger(__name__)


class STLModel(ForecastModel):
    """STL-decomposition-based forecast.

    Trend is extrapolated via linear regression on the decomposed trend
    component; the seasonal sub-series pattern is repeated forward.
    """

    name: str = "stl"
    min_history: int = 14  # must be >= 2 * seasonal_period

    def __init__(self, seasonal_period: int = 7) -> None:
        self._seasonal_period = seasonal_period
        self._trend_slope: float | None = None
        self._trend_intercept: float | None = None
        self._seasonal_pattern: np.ndarray | None = None
        self._fitted: bool = False

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
        period = self._seasonal_period
        if len(y) < max(self.min_history, 2 * period):
            raise ModelFitError(
                f"STL requires at least {max(self.min_history, 2 * period)} "
                f"non-null observations, got {len(y)}"
            )

        try:
            from statsmodels.tsa.seasonal import STL

            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=UserWarning)
                warnings.filterwarnings("ignore", category=FutureWarning)
                stl = STL(y.values, period=period, robust=True)
                result = stl.fit()
        except ImportError:
            # Fallback for older statsmodels — use seasonal_decompose
            from statsmodels.tsa.seasonal import seasonal_decompose

            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=UserWarning)
                result = seasonal_decompose(y.values, model="additive", period=period)
        except Exception as exc:
            raise ModelFitError(f"STL decomposition failed: {exc}") from exc

        trend = result.trend  # type: ignore[union-attr]
        seasonal = result.seasonal  # type: ignore[union-attr]

        # Drop NaN tails from decomposition
        mask = ~np.isnan(trend) & ~np.isnan(seasonal)
        if mask.sum() < period:
            raise ModelFitError(
                f"STL produced too few valid observations: {mask.sum()}"
            )
        trend_valid = trend[mask]
        seasonal_valid = seasonal[mask]

        # Linear extrapolation of trend
        x = np.arange(len(trend_valid))
        coeffs = np.polyfit(x, trend_valid, 1)
        self._trend_slope = float(coeffs[0])
        self._trend_intercept = float(coeffs[1])

        # Extract the last full seasonal cycle as the pattern
        # If we have fewer points, repeat what we have
        full_cycles = len(seasonal_valid) // period
        if full_cycles >= 1:
            self._seasonal_pattern = seasonal_valid[-period:].copy()
        else:
            # Pad by repeating
            pat = seasonal_valid.copy()
            while len(pat) < period:
                pat = np.concatenate([pat, pat[: period - len(pat)]])
            self._seasonal_pattern = pat[:period]

        self._fitted = True

    # ------------------------------------------------------------------
    def forecast(self, h: int) -> pd.Series:
        if not self._fitted:
            raise ModelFitError("STL.forecast() called before fit()")
        if h < 1:
            raise ModelFitError(f"horizon must be >= 1, got {h}")

        assert self._trend_slope is not None
        assert self._trend_intercept is not None
        assert self._seasonal_pattern is not None

        n_trend = len(self._seasonal_pattern) + h  # approx — trend was fit on valid points
        # The trend indices for forecasting extend beyond the observed window
        last_trend_x = n_trend  # approximate; we fit on valid trend values
        trend_forecast = np.array([
            self._trend_intercept + self._trend_slope * (last_trend_x + i)
            for i in range(h)
        ])
        seasonal_forecast = np.array([
            self._seasonal_pattern[i % len(self._seasonal_pattern)]
            for i in range(h)
        ])
        values = trend_forecast + seasonal_forecast

        return pd.Series(values, name=self.name)
