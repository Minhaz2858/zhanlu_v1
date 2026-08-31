"""Cross-product panel XGBoost model with product embeddings.

Trains a single global XGBoost model across ALL products, using
product_id one-hot embeddings.  Learns cross-product spillover effects
(e.g., when a feedstock rises, related products usually follow with a short lag).

Deployed as a challenger in the champion/challenger system.  If it wins
for a product, it becomes the champion model for that product.

This model does NOT follow the per-product ForecastModel.fit(y) interface
directly — it requires a pooled multi-product dataset.  The nightly loop
calls ``fit_pooled()`` with data from all active targets, and then
``forecast()`` for each product individually.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.services.forecasting.models.base import ForecastModel, ModelFitError

logger = logging.getLogger(__name__)

# Minimum number of products and rows for the panel model
_MIN_PRODUCTS = 3
_MIN_TOTAL_ROWS = 200


@dataclass
class PanelTrainingData:
    """Container for pooled multi-product training data."""
    product_keys: list[str]
    combined_df: pd.DataFrame  # columns: product_id_*, lag_*, y
    product_onehot_columns: list[str]


class PanelXGBoost(ForecastModel):
    """Cross-product panel model using XGBoost with product one-hot embeddings.

    Usage::

        panel = PanelXGBoost()
        panel.fit_pooled(train_data)
        pred = panel.forecast(7, product_key="c5_cracked")
    """

    name: str = "panel_xgboost"
    min_history: int = 60

    def __init__(self, n_lags: int = 7, max_depth: int = 4, n_estimators: int = 100):
        self._n_lags = n_lags
        self._max_depth = max_depth
        self._n_estimators = n_estimators
        self._model = None
        self._product_keys: list[str] = []
        self._product_onehot_cols: list[str] = []
        self._last_values_by_product: dict[str, np.ndarray] = {}
        self._fitted = False

    # ------------------------------------------------------------------
    # Pooled fitting (not the standard per-product interface)
    # ------------------------------------------------------------------

    def fit_pooled(
        self,
        product_series: dict[str, pd.Series],
        exog_data: dict[str, pd.DataFrame | None] | None = None,
    ) -> bool:
        """Fit the panel model on pooled data from multiple products.

        Parameters
        ----------
        product_series : dict[str, pd.Series]
            Mapping of product_key → historical price series.
        exog_data : dict[str, pd.DataFrame | None] | None
            Optional exogenous features per product.

        Returns
        -------
        bool
            True if fitting succeeded, False otherwise.
        """
        try:
            import xgboost as xgb
        except ImportError:
            logger.warning("xgboost not installed — panel model cannot fit")
            return False

        if len(product_series) < _MIN_PRODUCTS:
            logger.warning(
                "PanelXGBoost: need ≥%d products, got %d — skipping",
                _MIN_PRODUCTS, len(product_series),
            )
            return False

        self._product_keys = sorted(product_series.keys())
        self._product_onehot_cols = [f"product_id_{pk}" for pk in self._product_keys]

        # Build pooled training data
        frames = []
        for pk, series in product_series.items():
            if len(series) < self._n_lags + 1:
                continue
            values = series.values.astype(float)
            for i in range(self._n_lags, len(values)):
                row: dict[str, Any] = {
                    "y": values[i],
                }
                # Lag features
                for lag in range(1, self._n_lags + 1):
                    row[f"lag_{lag}"] = values[i - lag]
                # Product one-hot
                for col in self._product_onehot_cols:
                    row[col] = 1.0 if col == f"product_id_{pk}" else 0.0
                # Diff features
                row["diff_1"] = values[i - 1] - values[i - 2] if i >= 2 else 0.0
                row["pct_change_1"] = (
                    (values[i - 1] - values[i - 2]) / abs(values[i - 2])
                    if i >= 2 and values[i - 2] != 0
                    else 0.0
                )
                frames.append(row)

            # Store last values for forecasting
            self._last_values_by_product[pk] = values[-(self._n_lags + 1):]

        if len(frames) < _MIN_TOTAL_ROWS:
            logger.warning(
                "PanelXGBoost: need ≥%d total rows, got %d — skipping",
                _MIN_TOTAL_ROWS, len(frames),
            )
            return False

        df = pd.DataFrame(frames)
        feature_cols = [c for c in df.columns if c != "y"]
        X = df[feature_cols].values.astype(float)
        Y = df["y"].values.astype(float)

        # Handle NaN/inf
        mask = np.isfinite(Y) & np.all(np.isfinite(X), axis=1)
        X, Y = X[mask], Y[mask]

        if len(Y) < _MIN_TOTAL_ROWS // 2:
            logger.warning("PanelXGBoost: too few valid rows after cleaning — skipping")
            return False

        self._model = xgb.XGBRegressor(
            n_estimators=self._n_estimators,
            max_depth=self._max_depth,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            verbosity=0,
        )
        self._model.fit(X, Y)
        self._feature_cols = feature_cols
        self._fitted = True

        logger.info(
            "PanelXGBoost: fitted on %d products, %d rows, %d features",
            len(product_series), len(Y), len(feature_cols),
        )
        return True

    # ------------------------------------------------------------------
    # Standard ForecastModel interface (per-product)
    # ------------------------------------------------------------------

    def fit(
        self,
        y: pd.Series,
        seasonal_period: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Per-product fit — stores last values for forecasting.

        The panel model must be pre-trained via ``fit_pooled()`` before
        calling this method. This method only records the last values
        for the specific product being forecasted.
        """
        product_key = kwargs.get("product_key", "")
        if not product_key:
            raise ModelFitError("PanelXGBoost.fit() requires product_key kwarg")

        if not self._fitted:
            raise ModelFitError(
                "PanelXGBoost must be pre-trained via fit_pooled() before per-product fit()"
            )

        if product_key not in self._product_keys:
            raise ModelFitError(
                f"PanelXGBoost: product '{product_key}' not in trained product set"
            )

        values = y.values.astype(float)
        if len(values) < self._n_lags:
            raise ModelFitError(
                f"PanelXGBoost: need ≥{self._n_lags} observations, got {len(values)}"
            )

        self._last_values_by_product[product_key] = values[-(self._n_lags + 1):]
        self._current_product = product_key

    def forecast(self, h: int, exog_future: pd.DataFrame | None = None) -> pd.Series:
        """Produce a point forecast for the current product."""
        if not self._fitted or self._model is None:
            raise ModelFitError("PanelXGBoost not fitted")

        product_key = getattr(self, "_current_product", "")
        if not product_key or product_key not in self._last_values_by_product:
            raise ModelFitError("No product selected — call fit() with product_key first")

        last_values = self._last_values_by_product[product_key]
        forecasts = []

        current_window = list(last_values[-(self._n_lags + 1):])

        for step in range(h):
            row: dict[str, Any] = {}
            # Lag features
            for lag in range(1, self._n_lags + 1):
                row[f"lag_{lag}"] = current_window[-(lag + 1)] if len(current_window) > lag else 0.0
            # Product one-hot
            for col in self._product_onehot_cols:
                row[col] = 1.0 if col == f"product_id_{product_key}" else 0.0
            # Diff features
            if len(current_window) >= 2:
                row["diff_1"] = current_window[-1] - current_window[-2]
                row["pct_change_1"] = (
                    (current_window[-1] - current_window[-2]) / abs(current_window[-2])
                    if current_window[-2] != 0
                    else 0.0
                )
            else:
                row["diff_1"] = 0.0
                row["pct_change_1"] = 0.0

            X_row = np.array([[row.get(c, 0.0) for c in self._feature_cols]], dtype=float)
            pred = float(self._model.predict(X_row)[0])

            if not math.isfinite(pred):
                pred = current_window[-1]  # fallback: last value

            forecasts.append(pred)
            current_window.append(pred)

        return pd.Series(forecasts, index=range(1, h + 1), name=product_key)

    @property
    def fitted(self) -> bool:
        return self._fitted

    @property
    def product_keys(self) -> list[str]:
        return list(self._product_keys)
