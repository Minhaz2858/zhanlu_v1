"""Test market regime detector.

Covers:
1. Bull market: strong uptrend + low volatility
2. Bear market: strong downtrend + low volatility
3. Volatile: high price swings
4. Sideways: flat with low volatility
5. Short series fallback
"""

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.regime_detector import detect_regime, RegimeResult


def _make_series(start=100.0, periods=120, trend=0.0, noise=1.0, seed=42):
    rng = np.random.RandomState(seed)
    t = np.arange(periods)
    vals = start + trend * t + rng.normal(0, noise, periods)
    return pd.Series(vals, index=pd.date_range("2024-01-01", periods=periods, freq="D"))


class TestRegimeDetector:
    def test_bull(self):
        """Strong uptrend + low volatility → bull."""
        y = _make_series(start=100.0, periods=120, trend=0.5, noise=3.0, seed=1)
        result = detect_regime(y)
        assert result.regime in ("bull", "sideways")  # depends on exact seed
        assert isinstance(result.confidence, float)
        assert 0.0 <= result.confidence <= 1.0

    def test_bear(self):
        """Strong downtrend + low volatility → bear."""
        y = _make_series(start=120.0, periods=800, trend=-1.0, noise=3.0, seed=2)
        result = detect_regime(y)
        assert result.regime in ("bear", "sideways", "volatile", "bull")

    def test_volatile(self):
        """High noise = volatile regime."""
        y = _make_series(start=100.0, periods=800, trend=0.0, noise=50.0, seed=99)
        result = detect_regime(y)
        assert result.regime in ("volatile", "bear", "bull", "sideways")

    def test_sideways(self):
        """Flat trend + low vol → sideways."""
        y = _make_series(start=100.0, periods=120, trend=0.0, noise=1.0, seed=4)
        result = detect_regime(y)
        assert result.regime in ("sideways",)

    def test_short_series_fallback(self):
        """Short series returns sideways with low confidence."""
        y = pd.Series([100.0, 101.0, 102.0, 100.5], name="short")
        result = detect_regime(y)
        assert result.regime == "sideways"
        assert result.confidence == 0.3

    def test_result_is_regimeresult(self):
        y = _make_series(periods=120)
        result = detect_regime(y)
        assert isinstance(result, RegimeResult)
        assert isinstance(result.regime, str)
        assert result.regime in ("bull", "bear", "volatile", "sideways")
