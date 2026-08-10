"""P3-4 tests: Purged K-fold CV with embargo."""
from __future__ import annotations

import numpy as np
import pandas as pd
from app.services.forecasting.purged_cv import evaluate_purged


class _DummyModel:
    """Minimal model that just repeats the last value."""
    name = "dummy"
    min_history = 2

    def fit(self, y, seasonal_period=None, **kwargs):
        self._last_val = float(y.iloc[-1])

    def forecast(self, h):
        return pd.Series([self._last_val] * h)


class _DummyModel2:
    """Slightly different dummy: last value + small drift."""
    name = "dummy2"
    min_history = 2

    def fit(self, y, seasonal_period=None, **kwargs):
        drift = (y.iloc[-1] - y.iloc[0]) / max(len(y) - 1, 1) if len(y) > 1 else 0
        self._last = float(y.iloc[-1])
        self._drift = drift

    def forecast(self, h):
        return pd.Series([self._last + self._drift * (i + 1) for i in range(h)])


def _ar_series(n=80, seed=42) -> pd.Series:
    rng = np.random.RandomState(seed)
    vals = [100.0]
    for i in range(1, n):
        vals.append(vals[-1] * 0.7 + rng.randn() * 2.0 + 30.0)
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.Series(vals, index=dates, name="y")


def test_evaluate_purged_returns_result():
    y = _ar_series(80)
    models = {"dummy": _DummyModel(), "dummy2": _DummyModel2()}
    result = evaluate_purged(y, models, n_folds=3, embargo=7, min_train=20, horizon=7)
    assert result.n_folds > 0, f"Expected folds > 0, got {result.n_folds}"
    assert len(result.per_fold_mape) > 0
    assert not np.isnan(result.mean_mape)


def test_embargo_reduces_leakage():
    """Larger embargo should not increase leakage score."""
    y = _ar_series(100, seed=1)
    models = {"dummy": _DummyModel()}
    r1 = evaluate_purged(y, models, n_folds=3, embargo=3, min_train=25, horizon=7)
    r2 = evaluate_purged(y, models, n_folds=3, embargo=10, min_train=25, horizon=7)
    assert r2.leakage_score <= r1.leakage_score + 0.2


def test_short_series_returns_graceful():
    y = _ar_series(15)
    models = {"dummy": _DummyModel()}
    result = evaluate_purged(y, models, n_folds=3, embargo=7, min_train=20, horizon=7)
    assert result.n_folds == 0
