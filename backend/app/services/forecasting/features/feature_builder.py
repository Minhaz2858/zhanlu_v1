"""Assemble the exogenous feature matrix (X) aligned to target y.

Training features: past actuals only (no leakage).
Horizon features: cascade values for feedstocks, flat for FX, zero for events.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from app.services.forecasting.features.feature_registry import FeatureSpec

logger = logging.getLogger(__name__)


@dataclass
class FeatureMatrix:
    X_train: pd.DataFrame | None
    X_future: pd.DataFrame | None
    feature_names: list[str] = field(default_factory=list)
    cleaning_note: str = ""


def build_features(
    target_product_key: str,
    y: pd.Series,
    spec: FeatureSpec,
    feedstock_loader,
    fx_loader,
    event_loader,
    horizon: int,
    cascade_forecasts: dict[str, list[float]] | None = None,
) -> FeatureMatrix:
    if not spec.feedstock_keys:
        return FeatureMatrix(X_train=None, X_future=None, feature_names=[])

    y_dates = y.index
    start_date = y_dates.min()
    fetch_start = start_date - timedelta(days=max(spec.feedstock_lags) + 7)
    end_date = y_dates.max()

    # Load feedstock actuals
    feedstock_dfs: dict[str, pd.DataFrame] = {}
    for fk in spec.feedstock_keys:
        try:
            df = feedstock_loader.read_actuals(fk, fetch_start, end_date)
            feedstock_dfs[fk] = df.set_index("ds")
        except Exception as exc:
            logger.warning("Failed to load feedstock %s: %s", fk, exc)

    if not feedstock_dfs:
        return FeatureMatrix(X_train=None, X_future=None, cleaning_note="feedstock load failed")

    # Load FX
    fx_df = None
    if spec.use_fx:
        try:
            fx_df = fx_loader.read_usd_cny(fetch_start, end_date).set_index("ds")
        except Exception as exc:
            logger.warning("Failed to load FX: %s", exc)

    # Load events
    try:
        event_df = event_loader.read_flags(target_product_key, fetch_start, end_date).set_index("ds")
    except Exception as exc:
        logger.warning("Failed to load events: %s", exc)
        event_df = pd.DataFrame({"event_flag": 0.0}, index=y_dates)

    # Build X_train
    train_rows: list[dict] = []
    train_dates: list = []
    for date in y_dates:
        row: dict = {}
        for fk, df in feedstock_dfs.items():
            for lag in spec.feedstock_lags:
                lag_date = date - timedelta(days=lag)
                col_name = f"{fk}_lag{lag}"
                if lag_date in df.index:
                    row[col_name] = float(df.loc[lag_date, fk])
                elif len(df) > 0:
                    prior = df[df.index <= lag_date]
                    row[col_name] = float(prior[fk].iloc[-1]) if len(prior) > 0 else float(df[fk].iloc[0])
                else:
                    row[col_name] = 0.0
        for (child, parent) in spec.spread_pairs:
            child_col = f"{child}_lag1"
            parent_col = f"{parent}_lag1"
            if child_col in row and parent_col in row:
                row[f"spread_{child}_{parent}"] = row[child_col] - row[parent_col]
        if spec.use_fx and fx_df is not None:
            if date in fx_df.index:
                row["usd_cny"] = float(fx_df.loc[date, "usd_cny"])
            elif len(fx_df) > 0:
                row["usd_cny"] = float(fx_df["usd_cny"].iloc[-1])
            else:
                row["usd_cny"] = 7.2
        if date in event_df.index:
            row["event_flag"] = float(event_df.loc[date, "event_flag"])
        else:
            row["event_flag"] = 0.0
        if spec.calendar_features:
            dow = date.weekday()
            row["dow_sin"] = float(np.sin(2 * np.pi * dow / 7))
            row["dow_cos"] = float(np.cos(2 * np.pi * dow / 7))
        train_rows.append(row)
        train_dates.append(date)

    X_train = pd.DataFrame(train_rows, index=train_dates)

    # Winsorize feedstock lag columns
    for col in X_train.columns:
        if "_lag" in col and len(X_train) >= 20:
            p1, p99 = np.percentile(X_train[col].dropna(), [1, 99])
            X_train[col] = X_train[col].clip(lower=p1, upper=p99)

    # Build X_future
    X_future = None
    if cascade_forecasts is not None and all(fk in cascade_forecasts for fk in spec.feedstock_keys):
        future_dates = pd.date_range(start=y_dates.max() + timedelta(days=1), periods=horizon, freq="D")
        future_rows: list[dict] = []
        for step, date in enumerate(future_dates):
            row: dict = {}
            for fk in spec.feedstock_keys:
                forecast_vals = cascade_forecasts[fk]
                for lag in spec.feedstock_lags:
                    col_name = f"{fk}_lag{lag}"
                    idx = step - lag
                    if idx >= 0:
                        row[col_name] = float(forecast_vals[idx])
                    elif idx < 0:
                        df = feedstock_dfs.get(fk)
                        if df is not None and len(df) > 0:
                            row[col_name] = float(df[fk].iloc[idx])
                        else:
                            row[col_name] = 0.0
            for (child, parent) in spec.spread_pairs:
                child_col = f"{child}_lag1"
                parent_col = f"{parent}_lag1"
                if child_col in row and parent_col in row:
                    row[f"spread_{child}_{parent}"] = row[child_col] - row[parent_col]
            if spec.use_fx and fx_df is not None and len(fx_df) > 0:
                row["usd_cny"] = float(fx_df["usd_cny"].iloc[-1])
            row["event_flag"] = 0.0
            if spec.calendar_features:
                dow = date.weekday()
                row["dow_sin"] = float(np.sin(2 * np.pi * dow / 7))
                row["dow_cos"] = float(np.cos(2 * np.pi * dow / 7))
            future_rows.append(row)
        X_future = pd.DataFrame(future_rows, index=future_dates)

    return FeatureMatrix(
        X_train=X_train,
        X_future=X_future,
        feature_names=list(X_train.columns),
    )
