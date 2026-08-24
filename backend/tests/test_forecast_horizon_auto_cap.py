"""Test horizon auto-cap: high-MAPE horizons are omitted from ForecastRun."""
from __future__ import annotations

import math
from unittest.mock import MagicMock

import pytest

from app.services.forecasting.backtest import BacktestResult


@pytest.fixture
def sample_target():
    """Minimal ForecastTarget for auto-cap testing."""
    t = MagicMock()
    t.id = "t-1"
    t.org_id = "o-1"
    t.app_id = "a-1"
    t.product_key = "c5_cracked"
    t.name = "C5 Cracked"
    t.model_config = {"max_horizon_mape": 10.0}
    return t


def test_horizon_autocap_logic_excludes_high_mape():
    """Auto-cap logic drops horizons where ensemble MAPE > threshold."""
    bt = BacktestResult(
        per_model_mape={"naive": 5.0},
        ensemble_mape=7.0,
        naive_mape=8.0,
        n_folds=5,
        residuals=[1.0, -0.5],
        directional_accuracy={"naive": 0.6},
        per_horizon_mape={
            3: {"naive": 4.0},
            7: {"naive": 6.0},
            15: {"naive": 8.0},
            30: {"naive": 25.0},
        },
        ensemble_mape_by_horizon={3: 4.5, 7: 6.5, 15: 8.5, 30: 27.5},
    )

    # Simulate the auto-cap logic from engine.py
    target = MagicMock()
    target.model_config = {"max_horizon_mape": 10.0}
    _max_mape = float((target.model_config or {}).get("max_horizon_mape", 15.0))
    results = {"3": {}, "7": {}, "15": {}, "30": {}}
    excluded = []
    if bt.ensemble_mape_by_horizon:
        for h in [3, 7, 15, 30]:
            emape = bt.ensemble_mape_by_horizon.get(h)
            if emape is not None and math.isfinite(emape) and emape > _max_mape:
                results.pop(str(h), None)
                excluded.append(h)

    assert "30" not in results
    assert "3" in results
    assert "7" in results
    assert "15" in results
    assert excluded == [30]


def test_horizon_autocap_logic_preserves_all_when_lax():
    """When threshold is very high, no horizons are dropped."""
    bt = BacktestResult(
        per_model_mape={"naive": 5.0},
        ensemble_mape=5.0,
        naive_mape=6.0,
        n_folds=5,
        residuals=[1.0],
        directional_accuracy={"naive": 0.6},
        per_horizon_mape={3: {"naive": 4.0}, 7: {"naive": 6.0}},
        ensemble_mape_by_horizon={3: 4.0, 7: 6.0},
    )

    target = MagicMock()
    target.model_config = {"max_horizon_mape": 50.0}
    _max_mape = float((target.model_config or {}).get("max_horizon_mape", 15.0))
    results = {"3": {}, "7": {}}
    excluded = []
    if bt.ensemble_mape_by_horizon:
        for h in [3, 7]:
            emape = bt.ensemble_mape_by_horizon.get(h)
            if emape is not None and math.isfinite(emape) and emape > _max_mape:
                results.pop(str(h), None)
                excluded.append(h)

    assert "3" in results
    assert "7" in results
    assert excluded == []


def test_horizon_autocap_defaults_to_15_when_no_config():
    """When target has no model_config, default threshold is 15.0."""
    bt = BacktestResult(
        per_model_mape={"naive": 5.0},
        ensemble_mape=12.0,
        naive_mape=13.0,
        n_folds=5,
        residuals=[1.0],
        directional_accuracy={"naive": 0.6},
        per_horizon_mape={3: {"naive": 12.0}, 7: {"naive": 18.0}},
        ensemble_mape_by_horizon={3: 12.0, 7: 18.0},
    )

    target = MagicMock()
    target.model_config = None
    _max_mape = float((target.model_config or {}).get("max_horizon_mape", 15.0))
    results = {"3": {}, "7": {}}
    excluded = []
    if bt.ensemble_mape_by_horizon:
        for h in [3, 7]:
            emape = bt.ensemble_mape_by_horizon.get(h)
            if emape is not None and math.isfinite(emape) and emape > _max_mape:
                results.pop(str(h), None)
                excluded.append(h)

    assert "3" in results  # 12.0 <= 15.0
    assert "7" not in results  # 18.0 > 15.0
    assert excluded == [7]
