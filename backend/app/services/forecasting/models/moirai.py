"""Moirai foundation model wrapper (multivariate, exog-aware, zero-shot).

Uses Salesforce/moirai-1.1-R-base (~91M params). Supports exogenous
covariates — reuses the existing FeatureSpec pipeline's X_train/X_future.

Lazy-imports torch + uni2ts. ImportError -> silently skipped by
build_model_pool, identical to the xgboost lazy-import pattern.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.services.forecasting.models.base import ForecastModel, ModelFitError

logger = logging.getLogger(__name__)

_MIN_HISTORY = 60


class MoiraiModel(ForecastModel):
    """Zero-shot multivariate forecaster using Moirai-1.1-R-base.

    Supports exogenous covariates via the existing FeatureSpec pipeline.
    Returns point (median) + quantile forecasts.

    Parameters
    ----------
    model_name : str
        HuggingFace model ID (default: ``Salesforce/moirai-1.1-R-base``).
    """

    name = "moirai"
    min_history = _MIN_HISTORY
    uses_exog: bool = False

    def __init__(self, model_name: str = "Salesforce/moirai-1.1-R-base"):
        self._model_name = model_name
        self._model = None
        self._y: pd.Series | None = None
        self._exog: pd.DataFrame | None = None
        self._exog_last: pd.DataFrame | None = None
        self._model_loaded = False

    def _load_model(self):
        """Lazy-load Moirai pipeline. Returns None if download fails."""
        if self._model is not None:
            return self._model
        if self._model_loaded:
            return None  # already tried and failed

        self._model_loaded = True
        try:
            import torch  # noqa: F401 — fail fast if torch missing
            from uni2ts.model.moirai import MoiraiForecast, MoiraiLinear

            self._model = MoiraiForecast(
                module=MoiraiLinear.from_pretrained(
                    self._model_name, local_files_only=True,
                ),
                prediction_length=7,
                context_length=512,
            )
            logger.info("Moirai pipeline loaded: %s", self._model_name)
            return self._model
        except (OSError, ValueError, FileNotFoundError) as exc:
            logger.warning(
                "Moirai model not available (offline/no-cache): %s — "
                "download with: huggingface-cli download %s",
                exc, self._model_name,
            )
            return None
        except ImportError:
            raise  # propagate for build_model_pool skip

    def fit(
        self,
        y: pd.Series,
        seasonal_period: int | None = None,
        exog: pd.DataFrame | None = None,
        **kwargs,
    ) -> None:
        y = y.dropna()
        if len(y) < self.min_history:
            raise ModelFitError(
                f"Moirai needs >= {self.min_history} points, got {len(y)}"
            )
        # Deferred load — first fit triggers download
        if self._model is None:
            self._model = self._load_model()
        self._y = y
        if exog is not None and len(exog) > 0:
            self.uses_exog = True
            self._exog = exog.reindex(y.index).ffill().fillna(0.0)
            self._exog_last = exog.iloc[-1:]

    def _run_forecast(
        self, h: int, exog_future: pd.DataFrame | None = None
    ) -> np.ndarray:
        """Run Moirai forecast and return samples array (num_samples, h)."""
        if self._y is None:
            raise ModelFitError("Moirai.forecast() called before fit()")

        target = self._y.values.astype(np.float32)
        past_feat = None
        fut_feat = None
        if self.uses_exog and self._exog is not None:
            exog_arr = self._exog.values.astype(np.float32)
            past_feat = [exog_arr]
            if exog_future is not None and len(exog_future) >= h:
                fut_feat = [exog_future.iloc[:h].values.astype(np.float32)]
            elif self._exog_last is not None:
                fut_feat = [
                    np.tile(
                        self._exog_last.values[0], (h, 1)
                    ).astype(np.float32)
                ]

        self._model.prediction_length = h

        # Build input — MoiraiForecastInput or similar
        # The exact API depends on uni2ts version; we use a dict fallback
        try:
            from uni2ts.model.moirai.input import MoiraiForecastInput

            forecast_input = MoiraiForecastInput(
                target=target,
                past_features=past_feat,
                future_features=fut_feat,
            )
            result = self._model(forecast_input)
        except (ImportError, TypeError):
            # Fallback: call model directly with target + features
            result = self._model(
                target=target,
                past_features=past_feat,
                future_features=fut_feat,
                prediction_length=h,
            )

        # Extract samples (num_samples, h)
        if hasattr(result, "samples"):
            samples = result.samples
            if hasattr(samples, "numpy"):
                samples = samples.numpy()
            else:
                samples = np.asarray(samples)
        else:
            # Fallback: treat result as samples directly
            samples = np.asarray(result)

        if samples.ndim == 3:
            samples = samples.squeeze(axis=0)  # squeeze batch dim
        return samples

    def forecast(
        self, h: int, exog_future: pd.DataFrame | None = None
    ) -> pd.Series:
        samples = self._run_forecast(h, exog_future)
        median = (
            np.median(samples, axis=0)
            if samples.ndim == 2
            else samples
        )
        return pd.Series(median, index=range(h), name=self.name)

    def forecast_quantiles(
        self,
        h: int,
        quantiles: list[float] | None = None,
        exog_future: pd.DataFrame | None = None,
    ) -> dict[float, pd.Series] | None:
        if quantiles is None:
            quantiles = [0.1, 0.5, 0.9]
        samples = self._run_forecast(h, exog_future)
        if samples.ndim != 2:
            return None
        return {
            q: pd.Series(
                np.quantile(samples, q, axis=0),
                index=range(h),
                name=f"{self.name}_q{q}",
            )
            for q in quantiles
        }
