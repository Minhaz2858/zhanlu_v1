"""Verify Wave 1 dormant flag activation — all pipeline paths run without error.

These tests confirm that activating 6 dormant flags does NOT break the
forecasting pipeline.  Each ops module has its own detailed unit tests;
this file validates that the flag-gated codepaths are reachable and that
the engine survives a light integration smoke test.
"""

import os

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.models import build_model_pool


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _flag(name: str) -> bool:
    return os.getenv(name, "false").lower() == "true"


FLAGS_EXPECTED_ON = [
    "FORECAST_DOMAIN_SIGNALS_ENABLED",
    "FORECAST_BIAS_CORRECTION_ENABLED",
    "FORECAST_ENHANCED_PREPROCESS_ENABLED",
    "FORECAST_EVENT_CALIBRATION_ENABLED",
    "FORECAST_ACCURACY_FEEDBACK_ENABLED",
    "FORECAST_THRESHOLD_AUTOTUNE_ENABLED",
]


# ---------------------------------------------------------------------------
# 1.  Environment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flag", FLAGS_EXPECTED_ON)
def test_flag_is_active(flag: str):
    """Dormant flag MUST be true after Wave 1 activation."""
    assert _flag(flag), f"{flag} is OFF — Wave 1 activation incomplete"


def test_monotonicity_stays_off():
    """Monotonicity is intentionally OFF (inappropriate for price forecasting)."""
    assert _flag("FORECAST_MONOTONICITY_ENABLED") is False


# ---------------------------------------------------------------------------
# 2.  Model pool stability
# ---------------------------------------------------------------------------

def test_model_pool_no_crash_after_flag_changes():
    """build_model_pool must succeed with all new flags active."""
    pool = build_model_pool()
    assert "naive_last" in pool
    assert "xgboost_exog" in pool
    # Foundation models are not expected yet (Wave 4 — flag still OFF)


# ---------------------------------------------------------------------------
# 3.  Flag-gated function smoke tests
# ---------------------------------------------------------------------------

def test_enhanced_preprocess_importable_and_callable():
    """preprocess_enhanced must be importable and run without raising."""
    from app.services.forecasting.preprocess_enhanced import preprocess_enhanced

    dates = pd.date_range("2024-01-01", periods=120, freq="D")
    y = pd.Series(
        np.random.RandomState(0).normal(100, 5, 120).round(2),
        index=dates, name="price",
    )
    result, report, holiday_df = preprocess_enhanced(
        y, "test_enhanced", seasonal_period=7,
        impute_missing=True, compute_anomaly_score=True,
        add_holiday_features=True,
    )
    assert hasattr(result, "y_clean")
    assert report is not None


def test_domain_signals_importable_and_callable():
    """compute_domain_signal_adjustment must run when flag ON without error."""
    from app.services.forecasting.domain_signals import (
        compute_domain_signal_adjustment,
    )

    from datetime import datetime

    rpt = compute_domain_signal_adjustment(
        product_id="isoprene",
        as_of_date=datetime.now(),
        naphtha_pct_change=0.0,
    )
    assert isinstance(rpt, dict)
    assert "total_pct" in rpt


def test_event_calibration_importable():
    from app.services.forecasting.ops.event_calibration import (
        run_event_calibration,
    )

    assert callable(run_event_calibration)


def test_accuracy_feedback_importable():
    from app.services.forecasting.ops.accuracy_feedback import (
        run_accuracy_feedback,
    )

    assert callable(run_accuracy_feedback)


def test_threshold_autotune_importable():
    from app.services.forecasting.ops.threshold_auto_tuner import (
        run_threshold_autotune,
    )

    assert callable(run_threshold_autotune)


def test_bias_correction_importable():
    from app.services.forecasting.ops.bias_correction import (
        apply_bias_correction,
    )

    assert callable(apply_bias_correction)
