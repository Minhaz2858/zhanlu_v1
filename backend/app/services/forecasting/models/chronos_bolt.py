"""Chronos-Bolt foundation model wrapper (univariate, zero-shot, probabilistic).

Uses Amazon's Chronos-Bolt-mini (~20M params) for CPU-friendly zero-shot
forecasting. Returns both point forecasts (median) and quantile forecasts
for probabilistic ensemble blending.

Lazy-imports torch + chronos. If either is missing, __init__ raises
ImportError, and build_model_pool() silently skips this model — identical
to the xgboost lazy-import pattern.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.services.forecasting.models.base import ForecastModel, ModelFitError

logger = logging.getLogger(__name__)

_MIN_HISTORY = 60


class ChronosBoltModel(ForecastModel):
    """Zero-shot univariate forecaster using Chronos-Bolt-mini.

    Parameters
    ----------
    model_name : str
        HuggingFace model ID (default: ``amazon/chronos-bolt-mini``).
    """

    name = "chronos_bolt"
    min_history = _MIN_HISTORY

    def __init__(self, model_name: str = "amazon/chronos-bolt-mini"):
        self._model_name = model_name
        self._pipeline = None
        self._y: pd.Series | None = None
        self._pipeline_loaded = False
        # Defer loading — download happens at first fit().
        # build_model_pool catches ImportError; download errors are
        # caught in _load_pipeline() with a warning + disable.

    def _load_pipeline(self):
        """Lazy-load the Chronos pipeline. Returns None if download fails."""
        if self._pipeline is not None:
            return self._pipeline
        if self._pipeline_loaded:
            return None  # already tried and failed

        self._pipeline_loaded = True
        try:
            import torch  # noqa: F401 — fail fast if torch missing
            from chronos import ChronosPipeline

            self._pipeline = ChronosPipeline.from_pretrained(
                self._model_name,
                device_map="cpu",
                torch_dtype=torch.float32,
                local_files_only=True,
            )
            logger.info("ChronosBolt pipeline loaded: %s", self._model_name)
            return self._pipeline
        except (OSError, ValueError) as exc:
            logger.warning(
                "ChronosBolt model not available (offline/no-cache): %s — "
                "download with: huggingface-cli download %s",
                exc, self._model_name,
            )
            return None
        except ImportError:
            raise  # propagate ImportError for build_model_pool skip

    def fit(
        self,
        y: pd.Series,
        seasonal_period: int | None = None,
        **kwargs,
    ) -> None:
        y = y.dropna()
        if len(y) < self.min_history:
            raise ModelFitError(
                f"ChronosBolt needs >= {self.min_history} points, got {len(y)}"
            )
        # Deferred load — first fit triggers download
        if self._pipeline is None:
            self._pipeline = self._load_pipeline()
        self._y = y

    def _predict_samples(self, h: int) -> np.ndarray:
        """Return raw forecast samples (num_samples, h)."""
        if self._pipeline is None:
            raise ModelFitError(
                "ChronosBolt model is not loaded. Pre-download the model with: "
                f"huggingface-cli download {self._model_name}"
            )
        import torch

        if self._y is None:
            raise ModelFitError("ChronosBolt.forecast() called before fit()")
        ctx = torch.tensor(
            self._y.values.astype(np.float32), dtype=torch.float32
        )
        forecast = self._pipeline.predict(ctx, prediction_length=h)
        # Chronos returns (batch=1, num_samples, h) — squeeze batch dim
        arr = forecast.numpy()
        if arr.ndim == 3:
            arr = arr.squeeze(axis=0)
        return arr

    def forecast(self, h: int, exog_future=None) -> pd.Series:
        samples = self._predict_samples(h)
        median = np.median(samples, axis=0)
        return pd.Series(median, index=range(h), name=self.name)

    def forecast_quantiles(
        self,
        h: int,
        quantiles: list[float] | None = None,
        exog_future: pd.DataFrame | None = None,
    ) -> dict[float, pd.Series] | None:
        if quantiles is None:
            quantiles = [0.1, 0.5, 0.9]
        samples = self._predict_samples(h)
        result = {}
        for q in quantiles:
            q_val = np.quantile(samples, q, axis=0)
            result[q] = pd.Series(
                q_val, index=range(h), name=f"{self.name}_q{q}"
            )
        return result
