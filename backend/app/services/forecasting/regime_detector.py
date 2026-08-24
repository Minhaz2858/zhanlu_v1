"""Market regime detector — classify price trend state.

Classifies the recent 30-day window into one of four regimes:
    bull, bear, volatile, sideways.

Used by the ensemble blender to apply regime-aware weight multipliers
so that models suited to the current market get more influence.

Flag-gated via FORECAST_REGIME_DETECTION_ENABLED (default false).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.config import settings

logger = logging.getLogger(__name__)

_REGIME_DETECTION_ENABLED = settings.FORECAST_REGIME_DETECTION_ENABLED

_LOOKBACK = 30        # rolling window length
_TREND_THRESH = 0.02  # ±2% return over lookback = trend
_VOL_HIGH_PCT = 75.0  # volatility above this percentile = "volatile"
_MIN_LENGTH = 40      # minimum series length


@dataclass
class RegimeResult:
    regime: str         # "bull", "bear", "volatile", "sideways"
    confidence: float   # 0.0 – 1.0
    volatility_30d: float
    rolling_return_30d: float


def detect_regime(y: pd.Series, lookback: int = _LOOKBACK) -> RegimeResult:
    """Detect the current market regime from recent price history.

    Args:
        y: Price series (can be a pandas Series or array-like).
        lookback: Number of most-recent observations to analyze.

    Returns:
        RegimeResult with regime label and confidence.
    """
    y = pd.Series(y).dropna().values.astype(float)
    if len(y) < _MIN_LENGTH:
        return RegimeResult(
            regime="sideways",
            confidence=0.3,
            volatility_30d=0.0,
            rolling_return_30d=0.0,
        )

    # Focus on the most recent `lookback` observations
    recent = y[-lookback:]
    historical = y[:-lookback] if len(y) > lookback else y

    # Compute rolling return (total return over window)
    if recent[0] == 0 or np.isnan(recent[0]):
        return RegimeResult(
            regime="sideways",
            confidence=0.3,
            volatility_30d=0.0,
            rolling_return_30d=0.0,
        )
    rolling_return = (recent[-1] - recent[0]) / recent[0]

    # Compute volatility (standard deviation of daily returns, annualized-like via √252)
    returns = np.diff(recent) / recent[:-1]
    vol_30d = float(np.std(returns))

    # Compute historical volatility distribution for relative comparison
    hist_returns = np.diff(historical) / historical[:-1]
    if len(hist_returns) < 10:
        hist_vol = vol_30d
    else:
        hist_vol_series = np.array([
            float(np.std(hist_returns[i:i + lookback]))
            for i in range(0, len(hist_returns) - lookback + 1, max(1, lookback // 4))
        ])
        if len(hist_vol_series) == 0:
            hist_vol = vol_30d
        else:
            hist_vol = float(np.percentile(hist_vol_series, _VOL_HIGH_PCT))

    is_high_vol = vol_30d > max(hist_vol * 1.2, 0.001)  # 20% above historical
    is_trending = abs(rolling_return) > _TREND_THRESH

    # Classification
    if is_high_vol:
        regime = "volatile"
        confidence = min(1.0, vol_30d / (hist_vol + 1e-10) / 2.0)
    elif is_trending:
        if rolling_return > 0:
            regime = "bull"
        else:
            regime = "bear"
        confidence = min(1.0, abs(rolling_return) / (_TREND_THRESH * 2.0))
    else:
        regime = "sideways"
        confidence = 1.0 - min(1.0, abs(rolling_return) / _TREND_THRESH)

    return RegimeResult(
        regime=regime,
        confidence=round(confidence, 3),
        volatility_30d=round(vol_30d, 6),
        rolling_return_30d=round(rolling_return, 4),
    )
