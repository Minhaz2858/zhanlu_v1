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
    volume_df: pd.DataFrame | None = None,
    operating_rate_df: pd.DataFrame | None = None,
    inventory_df: pd.DataFrame | None = None,
    import_price_df: pd.DataFrame | None = None,
    demand_signal=None,  # DemandSignal | None
    supplier_dispersion_df: pd.DataFrame | None = None,
    # Wave 5: new feature engineering
    tech_indicators_enabled: bool = False,
    fourier_enabled: bool = False,
    # P1-2A: cross-product lag features
    upstream_series_map: dict[str, pd.Series] | None = None,
    cross_product_lags_enabled: bool = False,
    # P3-2C: self-accuracy feature
    recent_mape_7d: float | None = None,
    self_accuracy_feature_enabled: bool = False,
) -> FeatureMatrix:
    # ---- preprocess volume_df (Wave 1: ERP volume exogenous) ----
    _volume_enabled = False
    if volume_df is not None and not volume_df.empty:
        if not isinstance(volume_df.index, pd.DatetimeIndex):
            if "date" in volume_df.columns:
                volume_df = volume_df.set_index("date")
            else:
                logger.warning("volume_df has no DatetimeIndex and no 'date' col; skipping ERP volume features.")
                volume_df = None
        if volume_df is not None:
            volume_df = volume_df.sort_index()
            if len(volume_df) < 8:
                logger.warning(
                    "volume_df has only %d rows (< 8); skipping ERP volume features.", len(volume_df)
                )
                volume_df = None
            else:
                _volume_enabled = True

    # ---- preprocess Wave 3 external exog DataFrames ----
    # Each tuple: (df, metric_col, lag_col_prefix)
    _exog_specs = [
        (operating_rate_df, "op_rate", "op_rate_lag"),
        (inventory_df, "inventory_t", "inventory_lag"),
        (import_price_df, "import_price_cny", "import_price_lag"),
    ]
    _exog_dfs: dict[str, tuple[pd.DataFrame, str]] = {}
    for raw_df, metric_col, prefix in _exog_specs:
        if raw_df is None or raw_df.empty:
            continue
        if not isinstance(raw_df.index, pd.DatetimeIndex):
            if "date" in raw_df.columns:
                raw_df = raw_df.set_index("date")
            else:
                logger.warning(
                    "%s has no DatetimeIndex and no 'date' col; skipping.",
                    prefix,
                )
                continue
        raw_df = raw_df.sort_index()
        if len(raw_df) < 8:
            logger.warning(
                "%s has only %d rows (< 8); skipping.", prefix, len(raw_df),
            )
            continue
        # Use the metric column if present; otherwise default to single value col
        if metric_col in raw_df.columns:
            _exog_dfs[prefix] = (raw_df, metric_col)
        else:
            logger.warning(
                "%s missing metric col '%s'; skipping.", prefix, metric_col,
            )

    # ---- demand signal + supplier dispersion (P0-1) ----
    _use_demand_signal = demand_signal is not None
    if _use_demand_signal:
        _ds_vol_momentum_4wk = demand_signal.vol_momentum_4wk if demand_signal.vol_momentum_4wk is not None else 0.0
        _ds_yoy_change_pct = demand_signal.yoy_change_pct if demand_signal.yoy_change_pct is not None else 0.0
        _ds_vol_price_divergence = demand_signal.vol_price_divergence if demand_signal.vol_price_divergence is not None else 0.0
        _ds_trend_encoded = {"rising": 1.0, "falling": -1.0}.get(demand_signal.demand_trend, 0.0)
    else:
        _ds_vol_momentum_4wk = 0.0
        _ds_yoy_change_pct = 0.0
        _ds_vol_price_divergence = 0.0
        _ds_trend_encoded = 0.0

    _use_supplier = supplier_dispersion_df is not None and not supplier_dispersion_df.empty
    if _use_supplier:
        sdf = supplier_dispersion_df.sort_values("date")
        _supplier_spread = float(sdf["spread"].iloc[-1]) if len(sdf) > 0 else 0.0
        _supplier_count = float(sdf["supplier_count"].iloc[-1]) if len(sdf) > 0 else 0.0
    else:
        _supplier_spread = 0.0
        _supplier_count = 0.0

    # ---- Wave 5: Pre-compute technical indicators & Fourier features ----
    rsi_series: pd.Series | None = None
    macd_series: pd.Series | None = None
    bb_bw_series: pd.Series | None = None

    if tech_indicators_enabled and len(y) >= 30:
        try:
            from app.services.forecasting.features.technical_indicators import (
                compute_rsi, compute_macd, compute_bollinger,
            )
            rsi_series = compute_rsi(y, period=14)
            macd_series, _macd_sig, _macd_h = compute_macd(y)
            _bb_m, _bb_u, _bb_l, bb_bw_series = compute_bollinger(y)
            logger.debug("Tech indicators computed for %s: RSI+MACD+BB", target_product_key)
        except Exception:
            logger.debug("Tech indicator computation failed for %s", target_product_key)
            rsi_series = macd_series = bb_bw_series = None

    fourier_df: pd.DataFrame | None = None
    if fourier_enabled:
        try:
            from app.services.forecasting.features.fourier_features import compute_fourier_terms
            fourier_df = compute_fourier_terms(y, n_harmonics=3, target_period=7)
            logger.debug("Fourier features computed for %s: %d cols", target_product_key, len(fourier_df.columns))
        except Exception:
            logger.debug("Fourier feature computation failed for %s", target_product_key)
            fourier_df = None

    # ---- Wave 5: external upstream-price features ----
    # (The industry-specific upstream-price loader was removed —
    # feature engineering is now fully generic.)

    if not spec.feedstock_keys:
        # No feedstock features, but Wave 3 external exog may still contribute
        # (Wave 3 T3.4). Build minimal feature matrix from external exog only.
        if not (_volume_enabled or _exog_dfs):
            return FeatureMatrix(X_train=None, X_future=None, feature_names=[])
        # Fall through to the Wave-3-only feature matrix path below
        return _build_external_only_matrix(
            y, volume_df, _volume_enabled, _exog_dfs, horizon,
            _use_demand_signal, _ds_vol_momentum_4wk, _ds_yoy_change_pct, _ds_vol_price_divergence, _ds_trend_encoded,
            _use_supplier, _supplier_spread, _supplier_count,
            rsi_series=rsi_series, macd_series=macd_series, bb_bw_series=bb_bw_series,
            fourier_df=fourier_df,
            tech_indicators_enabled=tech_indicators_enabled,
            fourier_enabled=fourier_enabled,
        )

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
        # P3-2C: self-accuracy feature (recent MAPE as model confidence signal)
        if self_accuracy_feature_enabled and recent_mape_7d is not None:
            row["recent_mape_7d"] = float(recent_mape_7d)
        # P1-2A: cross-product upstream lag features
        if cross_product_lags_enabled and upstream_series_map:
            for upstream_key, upstream_series in upstream_series_map.items():
                for lag in range(1, 4):  # lag 1, 2, 3 days
                    lag_date = date - timedelta(days=lag)
                    col = f"upstream_{upstream_key}_lag{lag}"
                    if lag_date in upstream_series.index:
                        row[col] = float(upstream_series.loc[lag_date])
                    else:
                        prior = upstream_series[upstream_series.index <= lag_date]
                        row[col] = float(prior.iloc[-1]) if len(prior) > 0 else float(upstream_series.iloc[0])
        # ERP volume lags (Wave 1)
        if _volume_enabled:
            vol_series = volume_df["volume"]
            for lag in range(1, 8):
                lag_date = date - timedelta(days=lag)
                col = f"erp_volume_lag{lag}"
                if lag_date in volume_df.index:
                    row[col] = float(vol_series.loc[lag_date])
                else:
                    prior = vol_series[vol_series.index <= lag_date]
                    row[col] = float(prior.iloc[-1]) if len(prior) > 0 else 0.0
        # Wave 3 external exog lags
        for prefix, (exog_df, metric_col) in _exog_dfs.items():
            exog_series = exog_df[metric_col]
            for lag in range(1, 8):
                lag_date = date - timedelta(days=lag)
                col = f"{prefix}{lag}"
                if lag_date in exog_df.index:
                    row[col] = float(exog_series.loc[lag_date])
                else:
                    prior = exog_series[exog_series.index <= lag_date]
                    if len(prior) > 0:
                        row[col] = float(prior.iloc[-1])
                    else:
                        # No prior data — fall back to the first known exog
                        # value (carry-forward at start of series).
                        row[col] = float(exog_series.iloc[0])
        # P0-1 demand signal fields (constant per-row scalars)
        if _use_demand_signal:
            row["vol_momentum_4wk"] = _ds_vol_momentum_4wk
            row["yoy_change_pct"] = _ds_yoy_change_pct
            row["vol_price_divergence"] = _ds_vol_price_divergence
            row["demand_trend_encoded"] = _ds_trend_encoded
        # P0-1 supplier dispersion fields
        if _use_supplier:
            row["supplier_spread"] = _supplier_spread
            row["supplier_count"] = _supplier_count
        # Wave 5: technical indicators (pre-computed, no look-ahead bias)
        if tech_indicators_enabled:
            if rsi_series is not None and date in rsi_series.index:
                v = rsi_series[date]
                row["rsi_14"] = float(v) if (isinstance(v, (int, float, np.floating)) and pd.notna(v)) else 50.0
            else:
                row["rsi_14"] = 50.0
            if macd_series is not None and date in macd_series.index:
                v = macd_series[date]
                row["macd"] = float(v) if (isinstance(v, (int, float, np.floating)) and pd.notna(v)) else 0.0
            else:
                row["macd"] = 0.0
            if bb_bw_series is not None and date in bb_bw_series.index:
                v = bb_bw_series[date]
                row["bb_bandwidth"] = float(v) if (isinstance(v, (int, float, np.floating)) and pd.notna(v)) else 0.0
            else:
                row["bb_bandwidth"] = 0.0
        # Wave 5: Fourier features
        if fourier_enabled and fourier_df is not None and date in fourier_df.index:
            for col in fourier_df.columns:
                row[col] = float(fourier_df[col].loc[date])
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
            # P3-2C: self-accuracy feature for future horizon (carry last known value)
            if self_accuracy_feature_enabled and recent_mape_7d is not None:
                row["recent_mape_7d"] = float(recent_mape_7d)
            # P1-2A: cross-product upstream lag features for future horizon
            if cross_product_lags_enabled and upstream_series_map:
                _last_date = y_dates[-1]
                for upstream_key, upstream_series in upstream_series_map.items():
                    for lag in range(1, 4):
                        lag_date = date - timedelta(days=lag)
                        col = f"upstream_{upstream_key}_lag{lag}"
                        if lag_date <= _last_date:
                            if lag_date in upstream_series.index:
                                row[col] = float(upstream_series.loc[lag_date])
                            else:
                                prior = upstream_series[upstream_series.index <= lag_date]
                                row[col] = float(prior.iloc[-1]) if len(prior) > 0 else float(upstream_series.iloc[0])
                        else:
                            # Future: carry last known value forward
                            row[col] = float(upstream_series.iloc[-1]) if len(upstream_series) > 0 else 0.0
            # ERP volume lags for future (Wave 1)
            if _volume_enabled:
                vol_series = volume_df["volume"]
                last_vol_date = volume_df.index.max()
                for lag in range(1, 8):
                    lag_date = date - timedelta(days=lag)
                    col = f"erp_volume_lag{lag}"
                    if lag_date <= last_vol_date:
                        if lag_date in volume_df.index:
                            row[col] = float(vol_series.loc[lag_date])
                        else:
                            prior = vol_series[vol_series.index <= lag_date]
                            row[col] = float(prior.iloc[-1]) if len(prior) > 0 else float(vol_series.iloc[0])
                    else:
                        row[col] = float(vol_series.iloc[-1])
            # Wave 3 external exog lags for future (carry last value forward)
            for prefix, (exog_df, metric_col) in _exog_dfs.items():
                exog_series = exog_df[metric_col]
                last_exog_date = exog_df.index.max()
                for lag in range(1, 8):
                    lag_date = date - timedelta(days=lag)
                    col = f"{prefix}{lag}"
                    if lag_date <= last_exog_date:
                        if lag_date in exog_df.index:
                            row[col] = float(exog_series.loc[lag_date])
                        else:
                            prior = exog_series[exog_series.index <= lag_date]
                            row[col] = float(prior.iloc[-1]) if len(prior) > 0 else float(exog_series.iloc[0])
                    else:
                        row[col] = float(exog_series.iloc[-1])
            # P0-1 demand signal + supplier dispersion in future horizon (carry last known)
            if _use_demand_signal:
                row["vol_momentum_4wk"] = _ds_vol_momentum_4wk
                row["yoy_change_pct"] = _ds_yoy_change_pct
                row["vol_price_divergence"] = _ds_vol_price_divergence
                row["demand_trend_encoded"] = _ds_trend_encoded
            if _use_supplier:
                row["supplier_spread"] = _supplier_spread
                row["supplier_count"] = _supplier_count
            # Wave 5: technical indicators (carry last known value forward)
            if tech_indicators_enabled:
                last_date = y_dates[-1]
                if rsi_series is not None and last_date in rsi_series.index:
                    v = rsi_series[last_date]
                    row["rsi_14"] = float(v) if (isinstance(v, (int, float, np.floating)) and pd.notna(v)) else 50.0
                else:
                    row["rsi_14"] = 50.0
                if macd_series is not None and last_date in macd_series.index:
                    v = macd_series[last_date]
                    row["macd"] = float(v) if (isinstance(v, (int, float, np.floating)) and pd.notna(v)) else 0.0
                else:
                    row["macd"] = 0.0
                if bb_bw_series is not None and last_date in bb_bw_series.index:
                    v = bb_bw_series[last_date]
                    row["bb_bandwidth"] = float(v) if (isinstance(v, (int, float, np.floating)) and pd.notna(v)) else 0.0
                else:
                    row["bb_bandwidth"] = 0.0
            # Wave 5: Fourier features (extend positionally into future)
            if fourier_enabled:
                _future_position = len(y_dates) + step
                _period = 7
                for h_idx in range(1, 4):  # harmonics 1–3
                    row[f"fourier_sin_{h_idx}"] = float(np.sin(2 * np.pi * h_idx * _future_position / _period))
                    row[f"fourier_cos_{h_idx}"] = float(np.cos(2 * np.pi * h_idx * _future_position / _period))
            future_rows.append(row)
        X_future = pd.DataFrame(future_rows, index=future_dates)

    return FeatureMatrix(
        X_train=X_train,
        X_future=X_future,
        feature_names=list(X_train.columns),
    )


def _build_external_only_matrix(
    y: pd.Series,
    volume_df: pd.DataFrame | None,
    volume_enabled: bool,
    exog_dfs: dict[str, tuple[pd.DataFrame, str]],
    horizon: int,
    use_ds: bool = False,
    ds_vol_momentum: float = 0.0,
    ds_yoy: float = 0.0,
    ds_vol_price_div: float = 0.0,
    ds_trend: float = 0.0,
    use_supplier: bool = False,
    supplier_spread: float = 0.0,
    supplier_count: float = 0.0,
    # Wave 5: feature engineering
    rsi_series: pd.Series | None = None,
    macd_series: pd.Series | None = None,
    bb_bw_series: pd.Series | None = None,
    fourier_df: pd.DataFrame | None = None,
    tech_indicators_enabled: bool = False,
    fourier_enabled: bool = False,
) -> FeatureMatrix:
    """Build a minimal feature matrix from Wave 3 external exog only.

    Used when spec.feedstock_keys is empty but external exog dataframes are
    supplied (Wave 3 T3.4). Mirrors the lag-column generation in the main
    ``build_features`` path so test expectations are consistent.
    """
    y_dates = y.index
    train_rows: list[dict] = []
    train_dates: list = []
    for date in y_dates:
        row: dict = {}
        if volume_enabled:
            vol_series = volume_df["volume"]
            for lag in range(1, 8):
                lag_date = date - timedelta(days=lag)
                col = f"erp_volume_lag{lag}"
                if lag_date in volume_df.index:
                    row[col] = float(vol_series.loc[lag_date])
                else:
                    prior = vol_series[vol_series.index <= lag_date]
                    row[col] = float(prior.iloc[-1]) if len(prior) > 0 else 0.0
        for prefix, (exog_df, metric_col) in exog_dfs.items():
            exog_series = exog_df[metric_col]
            for lag in range(1, 8):
                lag_date = date - timedelta(days=lag)
                col = f"{prefix}{lag}"
                if lag_date in exog_df.index:
                    row[col] = float(exog_series.loc[lag_date])
                else:
                    prior = exog_series[exog_series.index <= lag_date]
                    if len(prior) > 0:
                        row[col] = float(prior.iloc[-1])
                    else:
                        # No prior data — fall back to first known exog value.
                        row[col] = float(exog_series.iloc[0])
        # P0-1 demand signal + supplier dispersion
        if use_ds:
            row["vol_momentum_4wk"] = ds_vol_momentum
            row["yoy_change_pct"] = ds_yoy
            row["vol_price_divergence"] = ds_vol_price_div
            row["demand_trend_encoded"] = ds_trend
        if use_supplier:
            row["supplier_spread"] = supplier_spread
            row["supplier_count"] = supplier_count
        # Wave 5: technical indicators
        if tech_indicators_enabled:
            if rsi_series is not None and date in rsi_series.index:
                v = rsi_series[date]
                row["rsi_14"] = float(v) if (isinstance(v, (int, float, np.floating)) and pd.notna(v)) else 50.0
            else:
                row["rsi_14"] = 50.0
            if macd_series is not None and date in macd_series.index:
                v = macd_series[date]
                row["macd"] = float(v) if (isinstance(v, (int, float, np.floating)) and pd.notna(v)) else 0.0
            else:
                row["macd"] = 0.0
            if bb_bw_series is not None and date in bb_bw_series.index:
                v = bb_bw_series[date]
                row["bb_bandwidth"] = float(v) if (isinstance(v, (int, float, np.floating)) and pd.notna(v)) else 0.0
            else:
                row["bb_bandwidth"] = 0.0
        # Wave 5: Fourier features
        if fourier_enabled and fourier_df is not None and date in fourier_df.index:
            for col in fourier_df.columns:
                row[col] = float(fourier_df[col].loc[date])
        train_rows.append(row)
        train_dates.append(date)

    X_train = pd.DataFrame(train_rows, index=train_dates)
    return FeatureMatrix(
        X_train=X_train,
        X_future=None,  # no feedstock cascade → no future horizon
        feature_names=list(X_train.columns),
    )
