"""Test: champion/challenger DB persistence + auto-promotion."""
import os
import pytest
import datetime as _dt
from unittest.mock import patch, MagicMock

os.environ.setdefault("FORECAST_CHAMPION_CHALLENGER_ENABLED", "true")


def test_run_nightly_creates_shadow_run_rows():
    """run_nightly_champion_challenger should persist ChallengerShadowRun rows."""
    from app.services.forecasting.ops.champion_challenger import run_nightly_champion_challenger

    db = MagicMock()

    target = MagicMock()
    target.id = "t-1"
    target.name = "PE_LLDPE"
    target.product_key = "PE_LLDPE"
    target.model_config = {}

    champ_log = MagicMock()
    champ_log.realized_mape = 8.5

    def mock_query(model):
        q = MagicMock()
        name = getattr(model, "__name__", "")
        if name == "ForecastTarget":
            q.filter.return_value.all.return_value = [target]
        elif name == "ForecastAccuracyLog":
            q.filter.return_value.order_by.return_value.first.return_value = champ_log
        elif name == "ChallengerShadowRun":
            q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        else:
            q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        return q

    db.query.side_effect = mock_query

    with patch("app.services.forecasting.models.stacking_meta.StackingMetaLearner") as MockStacker:
        mock_stacker = MockStacker.return_value
        mock_stacker.is_fitted = True
        mock_stacker.last_train_mape = 6.2

        result = run_nightly_champion_challenger(db)

    assert result["shadow_runs"] >= 1
    assert db.add.called


def test_auto_promotion_triggers_on_consecutive_wins():
    """When challenger wins 3+ consecutive nights, auto-promote."""
    from app.services.forecasting.ops.champion_challenger import run_nightly_champion_challenger

    db = MagicMock()

    target = MagicMock()
    target.id = "t-1"
    target.name = "PE_LLDPE"
    target.product_key = "PE_LLDPE"
    target.model_config = {}

    champ_log = MagicMock()
    champ_log.realized_mape = 8.5

    prev_runs = []
    for i in range(3):
        r = MagicMock()
        r.shadow_delta_mape = 2.3
        r.promoted = False
        prev_runs.append(r)

    def mock_query(model):
        q = MagicMock()
        name = getattr(model, "__name__", "")
        if name == "ForecastTarget":
            q.filter.return_value.all.return_value = [target]
        elif name == "ForecastAccuracyLog":
            q.filter.return_value.order_by.return_value.first.return_value = champ_log
        elif name == "ChallengerShadowRun":
            q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = prev_runs
        else:
            q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        return q

    db.query.side_effect = mock_query

    with patch("app.services.forecasting.models.stacking_meta.StackingMetaLearner") as MockStacker:
        mock_stacker = MockStacker.return_value
        mock_stacker.is_fitted = True
        mock_stacker.last_train_mape = 6.2

        result = run_nightly_champion_challenger(db)

    assert result["promotions"] >= 1
    assert target.model_config.get("ensemble_overrides") is not None


def test_no_promotion_when_challenger_loses():
    """When challenger doesn't beat champion, no promotion."""
    from app.services.forecasting.ops.champion_challenger import run_nightly_champion_challenger

    db = MagicMock()
    target = MagicMock()
    target.id = "t-1"
    target.name = "PE_HDPE"
    target.product_key = "PE_HDPE"
    target.model_config = {}

    champ_log = MagicMock()
    champ_log.realized_mape = 8.5

    prev_runs = []
    for i in range(3):
        r = MagicMock()
        r.shadow_delta_mape = -0.5
        r.promoted = False
        prev_runs.append(r)

    def mock_query(model):
        q = MagicMock()
        name = getattr(model, "__name__", "")
        if name == "ForecastTarget":
            q.filter.return_value.all.return_value = [target]
        elif name == "ForecastAccuracyLog":
            q.filter.return_value.order_by.return_value.first.return_value = champ_log
        else:
            q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = prev_runs
        return q

    db.query.side_effect = mock_query

    with patch("app.services.forecasting.models.stacking_meta.StackingMetaLearner") as MockStacker:
        mock_stacker = MockStacker.return_value
        mock_stacker.is_fitted = True
        mock_stacker.last_train_mape = 10.0

        result = run_nightly_champion_challenger(db)

    assert result["promotions"] == 0
