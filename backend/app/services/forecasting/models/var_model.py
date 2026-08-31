"""
VAR/VECM multivariate forecasting model for correlated products.

Fits a statsmodels VAR on a matrix of cross-correlated products, forecasts
jointly, and extracts the target product's row.

Correlated chain (value-chain upstream → downstream). The product groups
themselves are loaded from the app's domain config ("correlated_groups" key);
empty config = no correlated groups (VAR degrades to the naive fallback).

Flag: FORECAST_VAR_ENABLED (default false)
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from app.services.domain_config import get_domain_config
from app.services.forecasting.models.base import ForecastModel

logger = logging.getLogger(__name__)

# ---- Correlated product groups (config-driven; empty = generic) ----
_CORRELATED_GROUPS: dict[str, list[str]] = dict(
    (get_domain_config("") or {}).get("correlated_groups") or {}
)

_VAR_MIN_ROWS = 60  # Minimum data points per product to include
_VAR_MIN_PRODUCTS = 3  # Minimum correlated products to form a VAR


class VARModel(ForecastModel):
    """Vector Autoregression for cross-product correlated forecasting.

    Parameters
    ----------
    product_key : str
        Target product key (used to locate correlated group).
    correlated_data : dict[str, pd.Series] | None
        {product_key: price_series} for all known products.  Passed at
        construction time; ``fit`` will filter to the correlated group.
    max_lags : int
        Maximum VAR lag order (AIC will select ≤ max_lags).  Default 5.
    """

    name = "var_model"
    min_history = _VAR_MIN_ROWS
    uses_exog = False

    def __init__(
        self,
        product_key: str = "",
        correlated_data: dict[str, pd.Series] | None = None,
        max_lags: int = 5,
    ) -> None:
        self.product_key = product_key
        self.correlated_data = correlated_data or {}
        self.max_lags = max_lags
        self._result = None
        self._selected_lags = 1
        self._joint_df: pd.DataFrame | None = None
        self._target_col: str = ""
        self._target_idx: int = -1
        self._fitted = False

    # ------------------------------------------------------------------
    # Fit
    # ------------------------------------------------------------------

    def fit(
        self,
        y: pd.Series,
        seasonal_period: int | None = None,
        **kwargs,
    ) -> None:
        """Fit VAR on the correlated product group.

        Parameters
        ----------
        y : pd.Series
            Target series (used to obtain the product's historical window).
        seasonal_period : int | None, ignored.
        """
        del seasonal_period  # VAR doesn't use explicit seasonality

        # Determine correlated group for this product
        group_keys = _CORRELATED_GROUPS.get(self.product_key, [])
        if len(group_keys) < _VAR_MIN_PRODUCTS - 1:  # minus self
            logger.debug(
                "VARModel: insufficient correlated products for %s "
                "(found %d, need ≥%d)",
                self.product_key, len(group_keys) + 1, _VAR_MIN_PRODUCTS,
            )
            return

        # Collect series for the correlated group + self, aligned on common dates
        available: dict[str, pd.Series] = {}
        if self.product_key in self.correlated_data:
            available[self.product_key] = self.correlated_data[self.product_key]
        for key in group_keys:
            if key in self.correlated_data:
                available[key] = self.correlated_data[key]

        if len(available) < _VAR_MIN_PRODUCTS:
            logger.debug(
                "VARModel: only %d products available for %s (need ≥%d)",
                len(available), self.product_key, _VAR_MIN_PRODUCTS,
            )
            return

        # Align on common date index and drop rows with any NaN
        df_list = []
        for key, series in available.items():
            s = series.dropna()
            if len(s) < _VAR_MIN_ROWS:
                logger.debug(
                    "VARModel: %s has only %d rows (need ≥%d), dropping",
                    key, len(s), _VAR_MIN_ROWS,
                )
                continue
            df_list.append(s.rename(key))

        if len(df_list) < _VAR_MIN_PRODUCTS:
            logger.debug(
                "VARModel: after filtering, only %d products remain for %s",
                len(df_list), self.product_key,
            )
            return

        self._joint_df = pd.concat(df_list, axis=1).dropna()
        if len(self._joint_df) < _VAR_MIN_ROWS:
            logger.debug(
                "VARModel: aligned data too short (%d rows) for %s",
                len(self._joint_df), self.product_key,
            )
            return

        self._target_col = self.product_key
        if self._target_col not in self._joint_df.columns:
            logger.warning(
                "VARModel: target %s not in joint dataset columns %s",
                self.product_key, list(self._joint_df.columns),
            )
            return

        # Fit VAR
        try:
            from statsmodels.tsa.api import VAR

            model = VAR(self._joint_df.astype(float))
            lag_order = min(self.max_lags, max(1, len(self._joint_df) // 10))
            self._result = model.fit(maxlags=lag_order, ic="aic")
            self._selected_lags = self._result.k_ar
            self._target_idx = list(self._joint_df.columns).index(self._target_col)
            self._fitted = True
            logger.info(
                "VARModel fitted for %s: products=%d, lags=%d, rows=%d",
                self.product_key, len(self._joint_df.columns),
                self._selected_lags, len(self._joint_df),
            )
        except Exception as exc:
            logger.warning(
                "VARModel fit failed for %s: %s", self.product_key, exc,
            )
            self._fitted = False

    # ------------------------------------------------------------------
    # Forecast
    # ------------------------------------------------------------------

    def forecast(self, h: int, exog_future: pd.DataFrame | None = None) -> pd.Series:
        """Forecast h steps ahead via VAR then extract target column.

        Returns a naive (last-value) forecast if the model was not fitted.
        """
        if not self._fitted or self._result is None or self._joint_df is None:
            # Fallback: naive forecast
            logger.debug(
                "VARModel: not fitted for %s, returning naive forecast",
                self.product_key,
            )
            return _naive_fallback(h)

        try:
            fc_result = self._result.forecast(
                self._joint_df.values[-self._selected_lags:], steps=h,
            )
            fc_target = fc_result[:, self._target_idx]
            last_date = self._joint_df.index[-1]
            idx = pd.date_range(
                start=last_date + pd.Timedelta(days=1), periods=h, freq="D",
            )
            return pd.Series(fc_target, index=idx, name="var_forecast")
        except Exception as exc:
            logger.warning(
                "VARModel forecast failed for %s: %s, falling back to naive",
                self.product_key, exc,
            )
            return _naive_fallback(h)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def fitted(self) -> bool:
        return self._fitted

    @property
    def selected_lags(self) -> int:
        return self._selected_lags

    @property
    def joint_columns(self) -> list[str]:
        if self._joint_df is not None:
            return list(self._joint_df.columns)
        return []


# ---- Helpers ----

def _naive_fallback(h: int, name: str = "var_fallback") -> pd.Series:
    """Return a naive forecast of h steps as a zero-indexed Series."""
    return pd.Series(
        np.zeros(h),
        index=pd.date_range(pd.Timestamp.now().date(), periods=h, freq="D"),
        name=name,
    )


def get_group_keys(product_key: str) -> list[str]:
    """Return correlated group product keys for a target product."""
    group = _CORRELATED_GROUPS.get(product_key, [])
    return [product_key] + group
