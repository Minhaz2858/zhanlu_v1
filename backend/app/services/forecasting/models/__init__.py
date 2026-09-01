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
    import pandas as pd
    from app.services.forecasting.models.base import ForecastModel

# Minimum rows for ML models (XGBoost).  Below this, XGBoost returns
# inf% MAPE on short series — skip it and rely on statistical models.
_MIN_ML_ROWS = 90


def build_model_pool(
    seasonal_period: int | None = None,
    y: "pd.Series | None" = None,
    product_key: str = "",
    correlated_data: dict[str, "pd.Series"] | None = None,
) -> dict[str, "ForecastModel"]:
    """Return a dict of {model_name: model_instance} for all known models.

    Each instance is freshly constructed.  *seasonal_period* is passed
    to models that need it at init-time (e.g. SeasonalNaive, STL).
    Models that fail to import (e.g. xgboost not installed) are silently
    skipped with a warning log.

    When *y* is provided and ``len(y) < _MIN_ML_ROWS``, the XGBoost
    models are omitted (they return inf% MAPE on short series).

    Callers should still guard individual ``fit()`` / ``forecast()``
    calls with try/except — this factory skips unavailable *classes*,
    but fit-time failures (convergence) must be caught by the ensemble.
    """
    import logging

    from app.config import settings
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

    # Optional: xgboost (lazy import — skip if not installed OR series too short)
    skip_ml = y is not None and len(y) < _MIN_ML_ROWS
    if skip_ml:
        logger.info(
            "build_model_pool: skipping XGBoost (series length %d < %d)",
            len(y), _MIN_ML_ROWS,
        )
    else:
        try:
            from app.services.forecasting.models.xgboost_reg import XGBoostReg

            pool["xgboost_reg"] = XGBoostReg()
            # Separate entry for exogenous-aware flavour (M2)
            xgb_exog = XGBoostReg()
            xgb_exog.name = "xgboost_exog"
            pool["xgboost_exog"] = xgb_exog
        except ImportError:
            logger.warning("xgboost not installed — skipping xgboost_reg model")

    # Optional: direct multi-step XGBoost (Wave 3 — separate model per horizon)
    direct_enabled = settings.FORECAST_XGB_DIRECT_ENABLED
    if direct_enabled and not skip_ml:
        try:
            from app.services.forecasting.models.xgboost_direct import (
                XGBoostDirect,
            )

            pool["xgboost_direct"] = XGBoostDirect()
            logger.info("build_model_pool: added xgboost_direct")
        except ImportError:
            logger.warning("xgboost not installed — skipping xgboost_direct model")

    # Optional: foundation models (flag-gated, lazy import)
    foundation_enabled = settings.FORECAST_FOUNDATION_MODELS_ENABLED
    if foundation_enabled and y is not None and len(y) >= 60:
        # Chronos-Bolt (univariate zero-shot)
        chronos_on = settings.FORECAST_FOUNDATION_MODEL_CHRONOS_ENABLED
        if chronos_on:
            try:
                from app.services.forecasting.models.chronos_bolt import (
                    ChronosBoltModel,
                )

                pool["chronos_bolt"] = ChronosBoltModel()
                logger.info("build_model_pool: added chronos_bolt")
            except ImportError:
                logger.warning(
                    "build_model_pool: torch/chronos not installed "
                    "— skipping chronos_bolt"
                )

        # Moirai (exog-aware zero-shot)
        moirai_on = settings.FORECAST_FOUNDATION_MODEL_MOIRAI_ENABLED
        if moirai_on:
            try:
                from app.services.forecasting.models.moirai import MoiraiModel

                pool["moirai"] = MoiraiModel()
                logger.info("build_model_pool: added moirai")
            except ImportError:
                logger.warning(
                    "build_model_pool: torch/uni2ts not installed "
                    "— skipping moirai"
                )
    elif foundation_enabled and (y is None or len(y) < 60):
        logger.info(
            "build_model_pool: skipping foundation models (series too short)"
        )

    # Optional: VAR model (Wave 6 — cross-product multivariate)
    var_enabled = settings.FORECAST_VAR_ENABLED
    if var_enabled and y is not None and len(y) >= 60:
        try:
            from app.services.forecasting.models.var_model import VARModel
            from app.services.forecasting.models.var_model import _CORRELATED_GROUPS

            group_keys = _CORRELATED_GROUPS.get(product_key, [])
            if len(group_keys) >= _VAR_MIN_PRODUCTS - 1:
                pool["var_model"] = VARModel(
                    product_key=product_key,
                    correlated_data=correlated_data,
                )
                logger.info(
                    "build_model_pool: added var_model for %s (group=%s)",
                    product_key, group_keys,
                )
            else:
                logger.debug(
                    "build_model_pool: skipping var_model for %s "
                    "(only %d correlated products, need ≥%d)",
                    product_key, len(group_keys), _VAR_MIN_PRODUCTS - 1,
                )
        except ImportError:
            logger.warning("statsmodels not installed — skipping var_model")

    return pool
