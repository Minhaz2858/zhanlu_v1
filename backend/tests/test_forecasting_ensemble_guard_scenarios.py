"""Tests for ensemble.py, guard.py, and scenarios.py."""

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.ensemble import (
    EnsembleResult,
    blend,
    run_models,
)
from app.services.forecasting.guard import (
    GuardResult,
    evaluate_guard,
)
from app.services.forecasting.scenarios import (
    ScenarioResult,
    generate,
)
from app.services.forecasting.models import build_model_pool


# ── Helpers ───────────────────────────────────────────────────────────

def _make_sine(n: int = 200) -> pd.Series:
    np.random.seed(42)
    vals = (
        np.sin(2 * np.pi * np.arange(n) / 7) * 5
        + np.arange(n) * 0.03
        + np.random.normal(0, 0.1, n)
    )
    return pd.Series(vals, name="y")


def _make_flat_forecast(h: int, val: float = 10.0) -> pd.Series:
    return pd.Series([val] * h, name="flat")


# ── Ensemble tests ────────────────────────────────────────────────────

class TestEnsembleBlend:
    def test_blend_returns_ensemble_result(self):
        f1 = _make_flat_forecast(10, 5.0)
        f2 = _make_flat_forecast(10, 10.0)
        errors = {"a": 0.1, "b": 0.2}
        result = blend({"a": f1, "b": f2}, errors)
        assert isinstance(result, EnsembleResult)
        assert len(result.point_forecast) == 10

    def test_better_model_gets_higher_weight(self):
        f1 = _make_flat_forecast(10, 5.0)
        f2 = _make_flat_forecast(10, 10.0)
        # Model "a" has 10x lower error → should get higher weight
        errors = {"a": 0.01, "b": 0.10}
        result = blend({"a": f1, "b": f2}, errors)
        w_a = result.weights.get("a", 0)
        w_b = result.weights.get("b", 0)
        assert w_a > w_b, f"Expected w_a({w_a:.3f}) > w_b({w_b:.3f})"

    def test_weights_sum_to_one(self):
        f1 = _make_flat_forecast(10, 1.0)
        f2 = _make_flat_forecast(10, 2.0)
        f3 = _make_flat_forecast(10, 3.0)
        errors = {"a": 0.1, "b": 0.2, "c": 0.15}
        result = blend({"a": f1, "b": f2, "c": f3}, errors)
        total = sum(result.weights.values())
        assert abs(total - 1.0) < 0.001

    def test_inf_error_drops_model(self):
        f1 = _make_flat_forecast(10, 5.0)
        f2 = _make_flat_forecast(10, 10.0)
        errors = {"a": float("inf"), "b": 0.2}
        result = blend({"a": f1, "b": f2}, errors)
        # Model "a" should be dropped
        assert "a" in set(result.models_failed) or result.weights.get("a", 1) == 0

    def test_all_inf_errors_equal_weights(self):
        f1 = _make_flat_forecast(10, 5.0)
        f2 = _make_flat_forecast(10, 10.0)
        errors = {"a": float("inf"), "b": float("inf")}
        result = blend({"a": f1, "b": f2}, errors)
        w_a = result.weights.get("a", 0)
        w_b = result.weights.get("b", 0)
        assert abs(w_a - w_b) < 0.01

    def test_empty_forecasts_raises(self):
        with pytest.raises(ValueError):
            blend({}, {})

    def test_floor_weight_applied(self):
        # 3 models with very different errors
        f1 = _make_flat_forecast(10, 1.0)
        f2 = _make_flat_forecast(10, 2.0)
        f3 = _make_flat_forecast(10, 3.0)
        errors = {"a": 0.001, "b": 10.0, "c": 10.0}
        result = blend({"a": f1, "b": f2, "c": f3}, errors, floor=0.01)
        for w in result.weights.values():
            assert w >= 0.01, f"Weight {w:.4f} below floor"


class TestRunModels:
    def test_run_models_on_sine(self):
        y = _make_sine(200)
        models = build_model_pool(seasonal_period=7)
        forecasts, runs, failed = run_models(models, y, h=14, seasonal_period=7)
        assert len(runs) >= 3
        assert len(forecasts) == len(runs)
        assert isinstance(forecasts[runs[0]], pd.Series)

    def test_failed_models_not_in_forecasts(self):
        y = _make_sine(200)
        # Use just one model
        models = {"ets": build_model_pool()["ets"]}
        forecasts, runs, failed = run_models(models, y, h=14, seasonal_period=7)
        assert "ets" in runs
        assert "ets" in forecasts


# ── Guard tests ───────────────────────────────────────────────────────

class TestGuard:
    def test_ensemble_better_than_naive(self):
        ep = _make_flat_forecast(10, 5.0)
        np_fc = _make_flat_forecast(10, 6.0)
        gr = evaluate_guard(ep, np_fc, ensemble_mape=0.05, naive_mape=0.10)
        assert gr.below_naive_baseline is False
        assert gr.published_forecast is ep  # ensemble wins

    def test_naive_better_than_ensemble(self):
        ep = _make_flat_forecast(10, 5.0)
        np_fc = _make_flat_forecast(10, 6.0)
        gr = evaluate_guard(ep, np_fc, ensemble_mape=0.20, naive_mape=0.10)
        assert gr.below_naive_baseline is True
        # Falls back to naive forecast
        assert np.allclose(gr.published_forecast.values, np_fc.values)

    def test_equal_mape_triggers_guard(self):
        """MAPE tie → below_naive_baseline=True to be conservative."""
        ep = _make_flat_forecast(10, 5.0)
        np_fc = _make_flat_forecast(10, 6.0)
        gr = evaluate_guard(ep, np_fc, ensemble_mape=0.10, naive_mape=0.10)
        assert gr.below_naive_baseline is True

    def test_naive_inf_mape_uses_ensemble(self):
        ep = _make_flat_forecast(10, 5.0)
        np_fc = _make_flat_forecast(10, 6.0)
        gr = evaluate_guard(ep, np_fc, ensemble_mape=0.05, naive_mape=float("inf"))
        assert gr.below_naive_baseline is False

    def test_ensemble_inf_mape_falls_back(self):
        ep = _make_flat_forecast(10, 5.0)
        np_fc = _make_flat_forecast(10, 6.0)
        gr = evaluate_guard(ep, np_fc, ensemble_mape=float("inf"), naive_mape=0.10)
        assert gr.below_naive_baseline is True

    def test_returns_guard_result_type(self):
        gr = evaluate_guard(
            _make_flat_forecast(10, 1.0),
            _make_flat_forecast(10, 2.0),
            0.1, 0.2,
        )
        assert isinstance(gr, GuardResult)

    # ── Soft-blend gate tests (Phase 2B) ───────────────────────────────

    def test_soft_blend_marginal_loss_blends(self):
        """Ensemble MAPE within 2% margin → blends, not hard discards."""
        ep = _make_flat_forecast(10, 5.0)
        np_fc = _make_flat_forecast(10, 6.0)
        # naive=10%, ensemble=10.15% → within 2% margin (0.2% absolute)
        gr = evaluate_guard(
            ep, np_fc, ensemble_mape=0.1015, naive_mape=0.10,
            soft_blend_enabled=True, soft_blend_margin_pct=2.0,
        )
        assert gr.below_naive_baseline is False
        assert 0.0 < gr.blend_ratio < 1.0  # proportional blend
        # Blended forecast should be between pure ensemble and pure naive
        assert gr.published_forecast.iloc[0] != ep.iloc[0]
        assert gr.published_forecast.iloc[0] != np_fc.iloc[0]

    def test_soft_blend_hard_loss_still_discards(self):
        """Ensemble MAPE way above margin → hard discard still applies."""
        ep = _make_flat_forecast(10, 5.0)
        np_fc = _make_flat_forecast(10, 6.0)
        # naive=10%, ensemble=20% → way beyond 2% margin
        gr = evaluate_guard(
            ep, np_fc, ensemble_mape=0.20, naive_mape=0.10,
            soft_blend_enabled=True, soft_blend_margin_pct=2.0,
        )
        assert gr.below_naive_baseline is True
        assert gr.blend_ratio == 0.0
        assert np.allclose(gr.published_forecast.values, np_fc.values)

    def test_soft_blend_disabled_behaves_same_as_before(self):
        """With soft_blend_enabled=False, behavior is unchanged."""
        ep = _make_flat_forecast(10, 5.0)
        np_fc = _make_flat_forecast(10, 6.0)
        gr = evaluate_guard(
            ep, np_fc, ensemble_mape=0.1015, naive_mape=0.10,
            soft_blend_enabled=False,
        )
        assert gr.below_naive_baseline is True  # old behavior
        assert gr.blend_ratio == 0.0

    def test_soft_blend_equal_mape_blends_equally(self):
        """Equal MAPE within margin → blend_ratio ≈ 1.0 (close to ensemble)."""
        ep = _make_flat_forecast(10, 5.0)
        np_fc = _make_flat_forecast(10, 6.0)
        gr = evaluate_guard(
            ep, np_fc, ensemble_mape=0.10, naive_mape=0.10,
            soft_blend_enabled=True, soft_blend_margin_pct=2.0,
        )
        # Equal MAPE: excess=0 → blend_ratio=1.0 (won't be "below_naive")
        assert gr.below_naive_baseline is False
        assert gr.blend_ratio > 0.9

    def test_soft_blend_at_margin_boundary(self):
        """Ensemble exactly at margin boundary → blend_ratio ≈ 0."""
        ep = _make_flat_forecast(10, 5.0)
        np_fc = _make_flat_forecast(10, 6.0)
        # naive=10%, ensemble=10.2% (2% margin exactly)
        gr = evaluate_guard(
            ep, np_fc, ensemble_mape=0.102, naive_mape=0.10,
            soft_blend_enabled=True, soft_blend_margin_pct=2.0,
        )
        # At exact margin: blend_ratio = max(0, 1.0 - 0.002/0.002) = 0
        # But with >= check, at exact boundary it's still within margin
        # (excess=0.002, margin=0.002, excess <= margin → blend)
        assert gr.blend_ratio <= 0.01  # nearly pure naive


# ── Scenarios tests ───────────────────────────────────────────────────

class TestScenarios:
    def test_generate_all_horizons(self):
        fc = _make_flat_forecast(35, 10.0)
        residuals = [0.5, -0.3, 0.2, -0.1, 0.4, -0.5, 0.1, -0.2]
        sr = generate(fc, residuals=residuals, mape=0.05, horizons=[3, 7, 30])
        assert 3 in sr.horizons
        assert 7 in sr.horizons
        assert 30 in sr.horizons

    def test_base_bull_bear_have_correct_length(self):
        fc = _make_flat_forecast(35, 10.0)
        sr = generate(fc, mape=0.05, horizons=[7])
        h7 = sr.horizons[7]
        assert len(h7["base"]) == 7
        assert len(h7["bull"]) == 7
        assert len(h7["bear"]) == 7

    def test_bull_higher_than_base(self):
        fc = pd.Series(np.arange(1, 31, dtype=float))
        sr = generate(fc, residuals=[0.5]*10, mape=0.05, horizons=[30])
        h30 = sr.horizons[30]
        # Bull should be uniformly higher than base
        assert (h30["bull"].values >= h30["base"].values).all()

    def test_bear_lower_than_base(self):
        fc = pd.Series(np.arange(1, 31, dtype=float))
        # Residuals with negative 25th percentile → bear < base
        sr = generate(fc, residuals=[-2.0, -1.5, 1.0, 2.0]*5, mape=0.05, horizons=[30])
        h30 = sr.horizons[30]
        # Bear should be uniformly lower than base when 25th percentile < 0
        assert (h30["bear"].values <= h30["base"].values).all()

    def test_confidence_high(self):
        fc = _make_flat_forecast(30, 10.0)
        sr = generate(fc, mape=0.05)
        assert sr.confidence == "High"

    def test_confidence_medium(self):
        fc = _make_flat_forecast(30, 10.0)
        sr = generate(fc, mape=0.15)
        assert sr.confidence == "Medium"

    def test_confidence_low(self):
        fc = _make_flat_forecast(30, 10.0)
        sr = generate(fc, mape=0.30)
        assert sr.confidence == "Low"

    def test_confidence_none_mape(self):
        fc = _make_flat_forecast(30, 10.0)
        sr = generate(fc, mape=None)
        assert sr.confidence == "Medium"

    def test_short_forecast_padded(self):
        fc = pd.Series([1.0, 2.0, 3.0])
        sr = generate(fc, mape=0.05, horizons=[7])
        h7 = sr.horizons[7]
        assert len(h7["base"]) == 7

    def test_no_residuals_uses_sigma(self):
        fc = _make_flat_forecast(30, 10.0)
        sr = generate(fc, residuals=None, mape=0.05)
        assert sr.bounds_source == "sigma"

    def test_returns_scenario_result(self):
        sr = generate(_make_flat_forecast(30, 10.0), mape=0.05)
        assert isinstance(sr, ScenarioResult)

    def test_default_horizons(self):
        fc = _make_flat_forecast(35, 10.0)
        sr = generate(fc, mape=0.05)
        assert 3 in sr.horizons
        assert 7 in sr.horizons
        assert 30 in sr.horizons
