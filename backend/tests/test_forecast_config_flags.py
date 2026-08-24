"""Verify all forecast flags declared in config.py are consumed by code.

Regression: ensures no os.environ.get() orphan remains in the forecasting
module after the 2026-08-21 centralization sweep.
"""
from __future__ import annotations

import inspect

import pytest

from app.config import settings
from app.services.forecasting import engine
from app.services.forecasting.models import build_model_pool
from app.services.forecasting import decision_engine
from app.services.forecasting import preprocess
from app.services.forecasting.models import xgboost_reg
from app.services.forecasting import accuracy_report
from app.services.forecasting import regime_detector


# All forecast flags that were previously read via os.environ.get() and
# are now expected to live on the Settings pydantic class.
_FORECAST_FLAGS = [
    "FORECAST_ERP_SMOOTHING_ENABLED",
    "FORECAST_ERP_SMOOTHING_WINDOW",
    "FORECAST_REGIME_AWARE_POOL_ENABLED",
    "FORECAST_STACKING_ENABLED",
    "FORECAST_ENHANCED_PREPROCESS_ENABLED",
    "FORECAST_ERP_VOLUME_EXOG_ENABLED",
    "FORECAST_DEMAND_SIGNAL_EXOG_ENABLED",
    "FORECAST_EXTERNAL_EXOG_ENABLED",
    "FORECAST_OILCHEM_EXOG_ENABLED",
    "FORECAST_TECHNICAL_INDICATORS_ENABLED",
    "FORECAST_FOURIER_FEATURES_ENABLED",
    "FORECAST_REGIME_DETECTION_ENABLED",
    "FORECAST_ADVANCED_GUARD_ENABLED",
    "FORECAST_MONOTONICITY_ENABLED",
    "FORECAST_SOFT_GATE_ENABLED",
    "FORECAST_SOFT_GATE_MARGIN_PCT",
    "FORECAST_XGB_DIRECT_ENABLED",
    "FORECAST_FOUNDATION_MODELS_ENABLED",
    "FORECAST_FOUNDATION_MODEL_CHRONOS_ENABLED",
    "FORECAST_FOUNDATION_MODEL_MOIRAI_ENABLED",
    "FORECAST_VAR_ENABLED",
    "FORECAST_XGB_TUNING_ENABLED",
    "FORECAST_FEATURE_SELECTION_ENABLED",
    "FORECAST_ANOMALY_DETECTION_ENABLED",
    "FORECAST_BUY_THRESHOLD",
    "FORECAST_SELL_THRESHOLD",
    "FORECAST_BUY_MIN_CHANGE",
    "FORECAST_SELL_MIN_CHANGE",
    "FORECAST_EDGE_THRESHOLD",
    "FORECAST_P_HIGH_MARGIN",
    "FORECAST_DRIFT_BLEND_FACTOR",
    "FORECAST_ACCURACY_THRESHOLD_EXCELLENT",
    "FORECAST_ACCURACY_THRESHOLD_ACCEPTABLE",
    "FORECAST_ACCURACY_THRESHOLD_CRITICAL",
    "FORECAST_DEMAND_SIGNAL_ENABLED",
    "FORECAST_EXTERNAL_SIGNAL_ENABLED",
]


@pytest.mark.parametrize("flag", _FORECAST_FLAGS)
def test_flag_exists_on_settings(flag: str):
    """Every centralized flag must be a valid attribute on settings."""
    assert hasattr(settings, flag), f"{flag} missing from config.py Settings"


@pytest.mark.parametrize("flag", _FORECAST_FLAGS)
def test_flag_default_is_not_none(flag: str):
    """Defaults must be explicit (not None) so getattr never returns None unexpectedly."""
    val = getattr(settings, flag)
    assert val is not None, f"{flag} default is None — should be explicit bool/int/float"


def test_no_os_environ_get_in_engine():
    """engine.py must not contain os.environ.get for forecast flags."""
    src = inspect.getsource(engine)
    for flag in _FORECAST_FLAGS:
        assert flag not in src or "getattr" in src, (
            f"{flag} still referenced in engine.py via raw env lookup"
        )


def test_no_os_environ_get_in_decision_engine():
    """decision_engine.py must not contain os.environ.get for thresholds."""
    src = inspect.getsource(decision_engine)
    for flag in [
        "FORECAST_BUY_THRESHOLD",
        "FORECAST_SELL_THRESHOLD",
        "FORECAST_BUY_MIN_CHANGE",
        "FORECAST_SELL_MIN_CHANGE",
        "FORECAST_EDGE_THRESHOLD",
        "FORECAST_P_HIGH_MARGIN",
    ]:
        assert flag not in src or "getattr" in src or "settings." in src, (
            f"{flag} still referenced via raw env lookup"
        )
