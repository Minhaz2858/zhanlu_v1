"""Exponential Smoothing model via statsmodels ``ETSModel`` / ``ExponentialSmoothing``.

Tries additive trend+seasonal first, falls back to additive trend only,
then simple exponential smoothing.
"""

from __future__ import annotations

import logging
import warnings

import pandas as pd
from statsmodels.tsa.holtwinters import ExponentialSmoothing

from app.services.forecasting.models.base import ForecastModel, ModelFitError

logger = logging.getLogger(__name__)


class ETSModel(ForecastModel):
    """Holt-Winters Exponential Smoothing (statsmodels)."""

    name: str = "ets"
    min_history: int = 14

    def __init__(self) -> None:
        self._fitted_model: ExponentialSmoothing | None = None
        self._seasonal_period: int | None = None

    # ------------------------------------------------------------------
    def fit(
        self,
        y: pd.Series,
        seasonal_period: int | None = None,
        **kwargs,
    ) -> None:
        _ = kwargs
        y = y.dropna()
        if len(y) < self.min_history:
            raise ModelFitError(
                f"ETS requires at least {self.min_history} non-null "
                f"observations, got {len(y)}"
            )
        self._seasonal_period = seasonal_period

        # Candidate configs to try in order of preference
        configs: list[dict] = []

        if seasonal_period is not None and seasonal_period > 1 and len(y) >= 2 * seasonal_period:
            configs.append(
                {
                    "trend": "add",
                    "seasonal": "add",
                    "seasonal_periods": seasonal_period,
                    "label": "additive_trend_seasonal",
                }
            )

        configs.append({"trend": "add", "seasonal": None, "label": "additive_trend"})
        configs.append({"trend": None, "seasonal": None, "label": "simple"})

        last_error: Exception | None = None
        for cfg in configs:
            label = cfg.pop("label", "?")
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=UserWarning)
                    warnings.filterwarnings("ignore", category=FutureWarning)
                    model = ExponentialSmoothing(
                        y.values,
                        trend=cfg.get("trend"),
                        seasonal=cfg.get("seasonal"),
                        seasonal_periods=cfg.get("seasonal_periods"),
                    )
                    self._fitted_model = model.fit(optimized=True)
                logger.debug("ETS converged with config=%s", label)
                return
            except (ValueError, RuntimeError, Exception) as exc:
                logger.debug("ETS config=%s failed: %s", label, exc)
                last_error = exc
                continue

        raise ModelFitError(
            f"ETS failed to converge with any config: {last_error}"
        )

    # ------------------------------------------------------------------
    def forecast(self, h: int) -> pd.Series:
        if self._fitted_model is None:
            raise ModelFitError("ETS.forecast() called before fit()")
        if h < 1:
            raise ModelFitError(f"horizon must be >= 1, got {h}")

        pred = self._fitted_model.forecast(h)
        return pd.Series(pred, name=self.name)
