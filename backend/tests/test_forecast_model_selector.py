"""P3-2 tests: Automated model selection."""
from __future__ import annotations
from app.services.forecasting.model_selector import select_model_pool


def test_select_keeps_all_when_no_history():
    pool = {"naive": "n", "ets": "e", "arima": "a", "xgboost": "x"}
    result = select_model_pool(pool.copy(), "p1", rolling_mape=None)
    assert result == pool


def test_select_prunes_poor_model():
    pool = {"m_best": "b", "m_avg": "a", "m_bad": "c"}
    rolling = {
        "m_best": [2.0, 2.1, 1.9, 2.0, 2.2],
        "m_avg": [3.0, 3.5, 3.2, 3.8, 3.5],
        "m_bad": [10.0, 9.5, 10.2, 11.0, 10.5],
    }
    result = select_model_pool(pool.copy(), "p2", rolling_mape=rolling, lookback_days=5,
                               skill_ratio_threshold=0.3, min_models=2)
    assert "m_best" in result
    assert "m_bad" not in result  # Pruned


def test_min_models_enforced():
    pool = {"a": 1, "b": 2, "c": 3, "d": 4}
    rolling = {
        "a": [2.0, 2.0],
        "b": [2.5, 2.5],
        "c": [15.0, 15.0],
        "d": [20.0, 20.0],
    }
    result = select_model_pool(pool.copy(), "p3", rolling_mape=rolling, min_models=3, lookback_days=2)
    assert len(result) >= 3


def test_single_model_always_kept():
    pool = {"only": "x"}
    rolling = {"only": [100.0, 100.0]}
    result = select_model_pool(pool.copy(), "p4", rolling_mape=rolling)
    assert result == pool
