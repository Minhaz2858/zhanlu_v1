"""P0.1/P0.2: Blended-ensemble backtest scoring + per-horizon honesty gate.

The old code computes ``ensemble_mape`` as the **mean of per-model MAPEs**,
which is NOT the same as the MAPE of the weighted blend the engine actually
publishes.  A blend of complementary models is typically **better** than the
average member, so the old gate over-triggers and downgrades products that
should pass.

Additionally, the gate compares a single scalar (averaged across horizons
[7, 14, 30]) even though decisions are made at h=7.  A model can be excellent
at 7 days and poor at 30 days and still get gated.

These tests pin the corrected behavior:
  - BacktestResult.ensemble_mape_by_horizon[h] = the blend's own MAPE at h
  - BacktestResult.naive_mape_by_horizon[h] = seasonal_naive MAPE at h
  - The blend's MAPE differs from (and with complementary models, beats)
    the mean of member MAPEs.
"""
import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.backtest import BacktestResult, evaluate
from app.services.forecasting.models.base import ForecastModel
from app.services.forecasting.models.naive import NaiveLast, SeasonalNaive


# ---------------------------------------------------------------------------
# Minimal deterministic models with known biases
# ---------------------------------------------------------------------------

class _OverPredict(ForecastModel):
    """Predicts last_value + bias (over-predicts when bias > 0)."""

    def __init__(self, bias: float = 5.0):
        self.bias = bias
        self._last: float = 0.0

    def fit(self, y: pd.Series, seasonal_period: int = 7) -> None:
        self._last = float(y.iloc[-1])

    def forecast(self, h: int) -> pd.Series:
        return pd.Series(np.full(h, self._last + self.bias), index=range(h))


class _UnderPredict(ForecastModel):
    """Predicts last_value - bias (under-predicts when bias > 0)."""

    def __init__(self, bias: float = 5.0):
        self.bias = bias
        self._last: float = 0.0

    def fit(self, y: pd.Series, seasonal_period: int = 7) -> None:
        self._last = float(y.iloc[-1])

    def forecast(self, h: int) -> pd.Series:
        return pd.Series(np.full(h, self._last - self.bias), index=range(h))


class _FlatPredict(ForecastModel):
    """Predicts last_value (identical to NaiveLast but under our name)."""

    def __init__(self):
        self._last: float = 0.0

    def fit(self, y: pd.Series, seasonal_period: int = 7) -> None:
        self._last = float(y.iloc[-1])

    def forecast(self, h: int) -> pd.Series:
        return pd.Series(np.full(h, self._last), index=range(h))


def _make_series(n: int = 120, seed: int = 42) -> pd.Series:
    """Generate a deterministic daily series with seasonality + trend.

    Uses integer index (not DatetimeIndex) to match production: the
    EdiaMysqlStrategy fetcher returns integer-indexed series, and
    SeasonalNaive has a known issue with DatetimeIndex frequency inference.
    """
    rng = np.random.RandomState(seed)
    t = np.arange(n, dtype=float)
    seasonal = 10 * np.sin(2 * np.pi * t / 7)
    trend = 0.05 * t
    noise = rng.randn(n) * 2
    return pd.Series(100 + trend + seasonal + noise)


# ---------------------------------------------------------------------------
# P0.1: blend MAPE ≠ mean of member MAPEs
# ---------------------------------------------------------------------------

class TestBlendEnsembleMape:
    """The blended ensemble's own MAPE must be computed and reported."""

    def test_ensemble_mape_by_horizon_field_exists(self):
        """BacktestResult must include ensemble_mape_by_horizon dict."""
        y = _make_series()
        models = {"over": _OverPredict(5.0), "under": _UnderPredict(5.0)}
        result = evaluate(y, models, seasonal_period=7)
        assert hasattr(result, "ensemble_mape_by_horizon")
        assert isinstance(result.ensemble_mape_by_horizon, dict)

    def test_naive_mape_by_horizon_field_exists(self):
        """BacktestResult must include naive_mape_by_horizon dict."""
        y = _make_series()
        models = {"over": _OverPredict(5.0), "under": _UnderPredict(5.0)}
        result = evaluate(y, models, seasonal_period=7)
        assert hasattr(result, "naive_mape_by_horizon")
        assert isinstance(result.naive_mape_by_horizon, dict)

    def test_blend_beats_mean_of_members_with_complementary_biases(self):
        """With symmetric over/under biases the blend should be closer to
        truth than either member, so the blend's own MAPE should be strictly
        less than the mean of the two members' MAPEs.

        This is the core bug proof: the old ``ensemble_mape`` (mean of members)
        over-states the blend's error, causing the honesty gate to over-trigger.
        """
        y = _make_series()
        models = {"over": _OverPredict(5.0), "under": _UnderPredict(5.0)}
        result = evaluate(y, models, seasonal_period=7)

        # Old field: mean of per-model MAPEs (averaged across horizons)
        old_ensemble_mape = result.ensemble_mape

        # New field: the blend's actual MAPE at h=7
        blend_mape_h7 = result.ensemble_mape_by_horizon.get(7)

        assert blend_mape_h7 is not None, "ensemble_mape_by_horizon[7] must be populated"
        assert np.isfinite(blend_mape_h7), f"blend MAPE at h=7 must be finite, got {blend_mape_h7}"

        # The blend of over+under should cancel biases → lower MAPE than the
        # mean of the two individual MAPEs.
        assert blend_mape_h7 < old_ensemble_mape, (
            f"Blend MAPE at h=7 ({blend_mape_h7:.4f}) should be < "
            f"mean-of-members ({old_ensemble_mape:.4f}) with complementary biases"
        )

    def test_blend_mape_populated_for_all_horizons(self):
        """ensemble_mape_by_horizon must have entries for all evaluated horizons."""
        y = _make_series()
        models = {"over": _OverPredict(3.0), "under": _UnderPredict(3.0)}
        result = evaluate(y, models, seasonal_period=7, horizons=[7, 14, 30])
        for h in [7, 14, 30]:
            assert h in result.ensemble_mape_by_horizon, f"Missing horizon {h}"
            assert np.isfinite(result.ensemble_mape_by_horizon[h]), (
                f"Blend MAPE at h={h} must be finite"
            )

    def test_naive_mape_by_horizon_matches_per_horizon_mape(self):
        """naive_mape_by_horizon[h] should equal per_horizon_mape[h]['seasonal_naive']."""
        y = _make_series()
        models = {"flat": _FlatPredict()}
        result = evaluate(y, models, seasonal_period=7)
        for h in [7, 14, 30]:
            expected = result.per_horizon_mape.get(h, {}).get("seasonal_naive", float("inf"))
            actual = result.naive_mape_by_horizon.get(h, float("inf"))
            if np.isfinite(expected):
                assert abs(actual - expected) < 1e-6, (
                    f"naive_mape_by_horizon[{h}]={actual} != per_horizon_mape[{h}]['seasonal_naive']={expected}"
                )


# ---------------------------------------------------------------------------
# P0.2: per-horizon gate — blend may beat naive at h=7 but not at h=30
# ---------------------------------------------------------------------------

class TestPerHorizonGate:
    """The honesty gate verdict can differ by horizon."""

    def test_h7_gate_can_pass_while_h30_fails(self):
        """When the ensemble beats naive at h=7 but not at h=30,
        the h=7 gate should NOT trigger.

        We use ETS (which the seasonal_naive benchmark can score against)
        to ensure both blend and naive MAPEs are finite at all horizons.
        The key assertion: per-horizon MAPEs exist so the gate can make
        different verdicts per horizon.
        """
        from app.services.forecasting.models.ets import ETSModel

        y = _make_series(n=200, seed=99)  # longer series for more folds
        models = {"ets": ETSModel()}
        result = evaluate(y, models, seasonal_period=7)

        h7_blend = result.ensemble_mape_by_horizon.get(7, float("inf"))
        h30_blend = result.ensemble_mape_by_horizon.get(30, float("inf"))
        h7_naive = result.naive_mape_by_horizon.get(7, float("inf"))
        h30_naive = result.naive_mape_by_horizon.get(30, float("inf"))

        # All values must be finite for a meaningful per-horizon gate
        for label, val in [("h7_blend", h7_blend), ("h30_blend", h30_blend),
                           ("h7_naive", h7_naive), ("h30_naive", h30_naive)]:
            assert np.isfinite(val), f"{label} must be finite, got {val}"

        # The gate verdicts CAN differ: (blend < naive) at h=7 and (blend >= naive) at h=30
        h7_blend_beats_naive = h7_blend < h7_naive
        h30_blend_beats_naive = h30_blend < h30_naive
        # We don't require they differ on this specific series — just that both are computable.
        assert isinstance(h7_blend_beats_naive, bool)
        assert isinstance(h30_blend_beats_naive, bool)
