"""P2-2 tests: Diebold-Mariano test, bootstrap CI, horizon-weighted skill."""
from __future__ import annotations

import numpy as np
from app.services.forecasting.backtest_significance import (
    diebold_mariano_test,
    mape_bootstrap_ci,
    horizon_weighted_skill,
    SignificanceResult,
)


# ---------------------------------------------------------------------------
# Diebold-Mariano
# ---------------------------------------------------------------------------

def test_dm_equal_errors_tie():
    """Identical residual lists yield tie."""
    e = [1.0, -2.0, 3.0, -1.0, 2.0]
    r = diebold_mariano_test(e, e, horizon=1)
    assert r.better_model == "tie"
    assert not r.significant


def test_dm_a_better_significant():
    """Model A has clearly smaller errors → significant."""
    rng = np.random.RandomState(42)
    e_a = (rng.randn(60) * 0.5).tolist()
    e_b = (rng.randn(60) * 2.0).tolist()
    r = diebold_mariano_test(e_a, e_b, horizon=1)
    assert r.better_model == "model_a"
    assert r.significant
    assert r.p_value < 0.05


def test_dm_b_better_significant():
    """Model B has clearly smaller errors → significant."""
    rng = np.random.RandomState(42)
    e_b = (rng.randn(60) * 0.3).tolist()
    e_a = (rng.randn(60) * 3.0).tolist()
    r = diebold_mariano_test(e_a, e_b, horizon=1)
    assert r.better_model == "model_b"
    assert r.significant


def test_dm_similar_errors_tie():
    """Similar error magnitudes yield non-significant result."""
    rng = np.random.RandomState(42)
    base = rng.randn(80).tolist()
    e_a = [x + np.random.randn() * 0.1 for x in base]
    e_b = [x + np.random.randn() * 0.1 for x in base]
    r = diebold_mariano_test(e_a, e_b, horizon=1)
    # Should not declare significance for very similar errors
    assert r.better_model == "tie" or not r.significant


def test_dm_short_residuals_graceful():
    """Fewer than 5 residuals should return tie."""
    r = diebold_mariano_test([1.0, 2.0], [3.0, 4.0])
    assert r.better_model == "tie"
    assert not r.significant


def test_dm_returns_struct():
    """All fields of SignificanceResult are populated."""
    e = [1.0, -2.0, 3.0, -1.0, 2.0, 0.5, -0.5, 1.5]
    r = diebold_mariano_test(e, e)
    assert isinstance(r, SignificanceResult)
    assert r.model_a == "model_a"
    assert r.model_b == "model_b"
    assert isinstance(r.dm_statistic, float)
    assert isinstance(r.p_value, float)
    assert isinstance(r.significant, bool)
    assert r.better_model in ("model_a", "model_b", "tie")


# ---------------------------------------------------------------------------
# Bootstrap CI
# ---------------------------------------------------------------------------

def test_bootstrap_ci_bounds():
    """CI lower < upper."""
    rng = np.random.RandomState(42)
    errors = (rng.randn(100) * 5.0 + 10.0).tolist()
    lo, hi = mape_bootstrap_ci(errors)
    assert lo < hi
    assert lo > 0


def test_bootstrap_ci_short_returns_mean():
    """Single error returns (mean, mean)."""
    lo, hi = mape_bootstrap_ci([5.0])
    assert lo == hi


# ---------------------------------------------------------------------------
# Horizon-weighted skill
# ---------------------------------------------------------------------------

def test_skill_better_than_naive_positive():
    """Model with lower MAPE than naive gets positive skill."""
    per_h = {
        7: {"xgboost": 5.0, "seasonal_naive": 10.0},
        15: {"xgboost": 8.0, "seasonal_naive": 12.0},
    }
    skills = horizon_weighted_skill(per_h)
    assert skills["xgboost"] > 0, f"Expected positive skill, got {skills['xgboost']}"


def test_skill_worse_than_naive_negative():
    """Model with higher MAPE gets negative skill."""
    per_h = {
        7: {"arima": 15.0, "seasonal_naive": 10.0},
    }
    skills = horizon_weighted_skill(per_h)
    assert skills["arima"] < 0


def test_skill_equal_naive_zero():
    """Equal MAPE gives zero skill."""
    per_h = {
        7: {"model_x": 10.0, "seasonal_naive": 10.0},
    }
    skills = horizon_weighted_skill(per_h)
    assert abs(skills["model_x"]) < 0.001


def test_skill_custom_weights():
    """Custom horizon weights are respected."""
    per_h = {
        7: {"m1": 10.0, "seasonal_naive": 10.0},
        30: {"m1": 5.0, "seasonal_naive": 10.0},
    }
    # Give 100% weight to horizon 30
    skills_custom = horizon_weighted_skill(per_h, weights={7: 0.0, 30: 1.0})
    # m1 is better at h=30 (5 < 10) → positive skill
    assert skills_custom["m1"] > 0


def test_skill_empty_input():
    """Empty per_horizon_mape returns empty dict."""
    assert horizon_weighted_skill({}) == {}
