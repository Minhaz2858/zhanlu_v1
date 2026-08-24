"""Test: rebuild queue processor — needs_rebuild → retrain → active."""
import os
import pytest
from unittest.mock import patch, MagicMock

# Ensure flags are on for tests via settings mock
_SETTINGS_PATCH = {
    "FORECAST_ACCURACY_FEEDBACK_ENABLED": True,
}


def test_rebuild_step_picks_up_needs_rebuild_targets():
    """_run_rebuild_step should retrain targets with status='needs_rebuild'."""
    from app.services.scheduled_tasks import _run_rebuild_step

    db = MagicMock()

    target1 = MagicMock()
    target1.id = "t-1"
    target1.name = "PE_LLDPE"
    target1.status = "needs_rebuild"
    target1.model_config = {"accuracy_alert": {"mape": 15.0}}

    target2 = MagicMock()
    target2.id = "t-2"
    target2.name = "PE_HDPE"
    target2.status = "needs_rebuild"
    target2.model_config = None

    db.query.return_value.filter.return_value.limit.return_value.all.return_value = [target1, target2]
    db.query.return_value.filter.return_value.count.return_value = 0

    with patch("app.services.forecasting.engine.ForecastEngine") as MockEngine:
        mock_engine = MockEngine.return_value
        mock_engine.compute_target_anchored.return_value = None

        with patch("app.config.settings") as mock_settings:
            mock_settings.FORECAST_ACCURACY_FEEDBACK_ENABLED = True
            result = _run_rebuild_step(db)

    assert result["rebuilt"] == 2
    assert result["failed"] == 0
    assert target1.status == "active"
    assert target2.status == "active"


def test_rebuild_step_handles_retrain_failure():
    """If retrain fails, target keeps needs_rebuild status."""
    from app.services.scheduled_tasks import _run_rebuild_step

    db = MagicMock()
    target = MagicMock()
    target.id = "t-fail"
    target.name = "PE_PP"
    target.status = "needs_rebuild"
    target.model_config = None

    db.query.return_value.filter.return_value.limit.return_value.all.return_value = [target]
    db.query.return_value.filter.return_value.count.return_value = 1

    with patch("app.services.forecasting.engine.ForecastEngine") as MockEngine:
        mock_engine = MockEngine.return_value
        mock_engine.compute_target_anchored.side_effect = RuntimeError("OOM")

        with patch("app.config.settings") as mock_settings:
            mock_settings.FORECAST_ACCURACY_FEEDBACK_ENABLED = True
            result = _run_rebuild_step(db)

    assert result["rebuilt"] == 0
    assert result["failed"] == 1
    assert target.status == "needs_rebuild"


def test_rebuild_step_skipped_when_flag_off():
    """When FORECAST_ACCURACY_FEEDBACK_ENABLED is false, step is skipped."""
    from app.services.scheduled_tasks import _run_rebuild_step

    with patch("app.config.settings") as mock_settings:
        mock_settings.FORECAST_ACCURACY_FEEDBACK_ENABLED = False
        result = _run_rebuild_step(MagicMock())

    assert result.get("skipped") is True
