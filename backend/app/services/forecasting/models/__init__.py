"""Model registry — provides a factory that builds all available models.

Usage::

    from app.services.forecasting.models import build_model_pool
    models = build_model_pool(seasonal_period=7)
    for name, m in models.items():
        m.fit(series)
        pred = m.forecast(14)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.forecasting.models.base import ForecastModel


def build_model_pool(
    seasonal_period: int | None = None,
) -> dict[str, ForecastModel]:
    """Return a dict of {model_name: model_instance} for all known models.

    Each instance is freshly constructed.  *seasonal_period* is passed
    to models that need it at init-time (e.g. SeasonalNaive, STL).
    Models that fail to import (e.g. xgboost not installed) are silently
    skipped with a warning log.

    Callers should still guard individual ``fit()`` / ``forecast()``
    calls with try/except — this factory skips unavailable *classes*,
    but fit-time failures (convergence) must be caught by the ensemble.
    """
    import logging

    from app.services.forecasting.models.naive import NaiveLast, SeasonalNaive
    from app.services.forecasting.models.ets import ETSModel
    from app.services.forecasting.models.arima import ARIMAModel
    from app.services.forecasting.models.stl import STLModel
    from app.services.forecasting.models.mean_reversion import MeanReversion

    logger = logging.getLogger(__name__)
    pool: dict[str, ForecastModel] = {}

    # Always-included models
    pool["naive_last"] = NaiveLast()
    pool["seasonal_naive"] = SeasonalNaive(seasonal_period=seasonal_period or 7)
    pool["ets"] = ETSModel()
    pool["arima"] = ARIMAModel()
    pool["stl"] = STLModel(seasonal_period=seasonal_period or 7)
    pool["mean_reversion"] = MeanReversion()

    # Optional: xgboost (lazy import — skip if not installed)
    try:
        from app.services.forecasting.models.xgboost_reg import XGBoostReg

        pool["xgboost_reg"] = XGBoostReg()
    except ImportError:
        logger.warning("xgboost not installed — skipping xgboost_reg model")

    return pool
