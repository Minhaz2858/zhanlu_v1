"""Technical indicators for time-series forecasting feature engineering.

Provides RSI, MACD, and Bollinger Bands computation as features that
capture momentum, trend strength, and mean-reversion patterns beyond
raw lag features.

All functions return ``pd.Series`` aligned to the input index.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_rsi(y: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (momentum oscillator, 0–100).

    Parameters
    ----------
    y : pd.Series
        Price or value series.
    period : int
        Look-back window (default 14).
    """
    delta = y.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rs = rs.fillna(1.0)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi.name = "rsi"
    return rsi


def compute_macd(
    y: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Moving Average Convergence Divergence.

    Returns
    -------
    (macd_line, signal_line, histogram)
    """
    ema_fast = y.ewm(span=fast, adjust=False).mean()
    ema_slow = y.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    macd_line.name = "macd"
    signal_line.name = "macd_signal"
    histogram.name = "macd_hist"
    return macd_line, signal_line, histogram


def compute_bollinger(
    y: pd.Series, window: int = 20, n_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands (volatility envelope).

    Returns
    -------
    (middle, upper, lower, bandwidth%)
    """
    middle = y.rolling(window).mean()
    std = y.rolling(window).std()
    upper = middle + n_std * std
    lower = middle - n_std * std
    bw = ((upper - lower) / middle.replace(0, np.nan)) * 100.0
    middle.name = "bb_mid"
    upper.name = "bb_upper"
    lower.name = "bb_lower"
    bw.name = "bb_bw"
    return middle, upper, lower, bw


def add_technical_features(
    y: pd.Series,
    output: pd.DataFrame | None = None,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    bb_window: int = 20,
) -> pd.DataFrame:
    """Compute all technical indicators and merge into a DataFrame.

    Parameters
    ----------
    y : pd.Series
        Input price series.
    output : pd.DataFrame or None
        Existing feature DataFrame (features added as new columns).

    Returns
    -------
    pd.DataFrame
        DataFrame indexed like *y* with columns: rsi, macd, macd_signal,
        macd_hist, bb_mid, bb_upper, bb_lower, bb_bw.
    """
    if output is None:
        output = pd.DataFrame(index=y.index)

    rsi = compute_rsi(y, rsi_period)
    macd, macd_sig, macd_h = compute_macd(y, macd_fast, macd_slow, macd_signal)
    bb_m, bb_u, bb_l, bb_bw = compute_bollinger(y, bb_window)

    for series in [rsi, macd, macd_sig, macd_h, bb_m, bb_u, bb_l, bb_bw]:
        output[series.name] = series
    return output
