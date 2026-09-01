"""Abstract base class for all forecast models.

All models must implement ``fit(y: pd.Series, **kwargs) -> None`` and
``forecast(h: int) -> pd.Series``.  The ensemble wraps each call in
try/except so a crashing model is dropped for that series, never
causing a full pipeline failure.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class ModelFitError(Exception):
    """Raised when a model fails to fit (convergence, insufficient data, etc.).

    The ensemble catches this and drops the model for the current series.
    """


class ForecastModel(ABC):
    """Uniform interface for every forecasting algorithm in the pool.

    Subclasses set:
    * ``name`` – short string used as dict key in the ensemble (e.g.
      ``"naive_last"``, ``"arima"``).
    * ``min_history`` – minimum number of observations required before
      ``fit()`` is called.  The engine skips models whose
      ``min_history`` exceeds the series length.
    """

    name: str = "base"
    min_history: int = 2

    @abstractmethod
    def fit(
        self,
        y: pd.Series,
        seasonal_period: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Fit model parameters to the training series *y*.

        Must be called before :meth:`forecast`.  Implementations may
        raise :class:`ModelFitError` on convergence failure or
        insufficient data.
        """
        ...

    @abstractmethod
    def forecast(self, h: int) -> pd.Series:
        """Produce a point forecast of length *h*.

        Returns a ``pd.Series`` whose index represents future time-steps.
        Must be called after :meth:`fit`.
        """
        ...

    def forecast_quantiles(
        self,
        h: int,
        quantiles: list[float] | None = None,
        exog_future: pd.DataFrame | None = None,
    ) -> dict[float, pd.Series] | None:
        """Return quantile forecasts, or None if the model is not probabilistic.

        Default implementation returns None (non-probabilistic).
        Foundation model wrappers override this to return native quantile forecasts.

        Parameters
        ----------
        h : int
            Forecast horizon.
        quantiles : list[float] | None
            Quantile levels (e.g. [0.1, 0.5, 0.9]). If None, uses [0.1, 0.5, 0.9].
        exog_future : pd.DataFrame | None
            Future exogenous features (for Moirai).

        Returns
        -------
        dict[float, pd.Series] | None
            Mapping quantile -> forecast series. None for non-probabilistic models.
        """
        return None

    def __repr__(self) -> str:
        return f"<{self.name}>"
