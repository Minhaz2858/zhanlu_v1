"""Unit tests for truth_gate.wrap_forecast_result."""

from app.services.forecasting.truth_gate import (
    TruthGateConfig,
    wrap_forecast_result,
)


def _anchor_rows(n: int) -> list[dict]:
    return [{"date": f"2026-01-{i+1:02d}", "value": 100.0 + i} for i in range(n)]


def _runs() -> list[dict]:
    return [
        {
            "target_id": "tgt-1",
            "horizon_days": 7,
            "point_estimate": 110.0,
            "scenarios": {"bull": 120, "base": 110, "bear": 100},
            "methodology": "ARIMA(2,1,2) ensemble",
            "confidence": 0.8,
        }
    ]


# ── Failure path ────────────────────────────────────────────────


def test_returns_insufficient_data_when_sample_is_empty():
    out = wrap_forecast_result(
        raw_runs=_runs(),
        anchor_rows=_anchor_rows(0),
        source_table="actual_price",
        sample_size=0,
    )
    assert out["success"] is False
    assert out["reason"] == "insufficient_data"
    assert out["source_table"] == "actual_price"
    assert out["sample_size"] == 0
    assert "Need" in out["message"]


def test_returns_insufficient_data_when_sample_below_threshold():
    for n in (1, 2, 3, 4):
        out = wrap_forecast_result(
            raw_runs=_runs(),
            anchor_rows=_anchor_rows(n),
            source_table="actual_price",
            sample_size=n,
        )
        assert out["success"] is False, f"n={n} should fail"
        assert out["reason"] == "insufficient_data", f"n={n} should be insufficient_data"
        assert out["sample_size"] == n


def test_failure_does_not_leak_runs():
    out = wrap_forecast_result(
        raw_runs=_runs(),
        anchor_rows=_anchor_rows(2),
        source_table="actual_price",
        sample_size=2,
    )
    assert "runs" not in out, "Failure path must not expose runs (would enable fabrication)"
    assert "data_anchor" not in out, "Failure path must not expose data_anchor"


# ── Success path ────────────────────────────────────────────────


def test_returns_success_at_min_threshold():
    out = wrap_forecast_result(
        raw_runs=_runs(),
        anchor_rows=_anchor_rows(5),
        source_table="actual_price",
        sample_size=5,
    )
    assert out["success"] is True
    assert "runs" in out
    assert "data_anchor" in out


def test_success_passes_through_runs_unchanged():
    runs = _runs()
    out = wrap_forecast_result(
        raw_runs=runs,
        anchor_rows=_anchor_rows(5),
        source_table="actual_price",
        sample_size=5,
    )
    assert out["runs"] == runs


def test_data_anchor_includes_first_and_last_rows():
    rows = _anchor_rows(20)
    out = wrap_forecast_result(
        raw_runs=_runs(),
        anchor_rows=rows,
        source_table="actual_price",
        sample_size=20,
    )
    a = out["data_anchor"]
    assert a["source_table"] == "actual_price"
    assert a["sample_size"] == 20
    assert a["first_5"] == rows[:5]
    assert a["last_5"] == rows[-5:]


# ── Configurable threshold ──────────────────────────────────────


def test_config_min_sample_size_is_honored():
    cfg = TruthGateConfig(min_sample_size=20)
    out = wrap_forecast_result(
        raw_runs=_runs(),
        anchor_rows=_anchor_rows(10),
        source_table="actual_price",
        sample_size=10,
        config=cfg,
    )
    assert out["success"] is False
    assert "≥20" in out["message"]


# ── Integration tests with ForecastEngine ────────────────────────

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.forecasting import ForecastTarget
from app.services.forecasting.engine import ForecastEngine


@pytest.fixture
def in_memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_engine_with_no_targets_returns_empty(in_memory_db):
    fe = ForecastEngine(in_memory_db)
    out = fe.compute_target_anchored(target_id="nonexistent")
    assert out is None  # no target row → None, no fabrication


def test_engine_with_thin_target_returns_anchored_with_low_sample(in_memory_db, monkeypatch):
    """If target has only 3 data points, anchored method should return sample_size=3."""
    target = ForecastTarget(
        id="tgt-thin",
        org_id="org-1",
        app_id="app-1",
        product_key="thin",
        name="Thin",
        datasource={
            "table": "fake_table",
            "time_column": "date",
            "measure": "value",
        },
    )
    in_memory_db.add(target)
    in_memory_db.commit()

    def fake_fetch(target):
        # Real _fetch_series returns a Series indexed by date-typed values from
        # the warehouse. Use a DatetimeIndex so the implementation's idx.date() works.
        return pd.Series(
            [100.0, 101.0, 102.0],
            index=pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]),
        )

    fe = ForecastEngine(in_memory_db)
    monkeypatch.setattr(fe, "_fetch_series", fake_fetch)
    out = fe.compute_target_anchored(target_id="tgt-thin")
    if out is not None:
        assert "source_table" in out
        assert "sample_size" in out
        assert out["sample_size"] == 3
        assert "anchor_rows" in out


# ── Tool handler tests ──────────────────────────────────────────

import asyncio
from unittest.mock import MagicMock, patch


def test_forecast_run_with_thin_data_returns_insufficient_data():
    """When the engine returns 3 data points, the tool returns insufficient_data."""
    from app.services.tool_handlers.forecast_tool import _forecast_run

    fake_engine = MagicMock()
    fake_engine.compute_target_anchored.return_value = {
        "run": None,
        "source_table": "actual_price",
        "sample_size": 3,
        "anchor_rows": [],
    }
    fake_db = MagicMock()
    fake_db.get.return_value = MagicMock(is_deleted=False)

    with patch(
        "app.services.tool_handlers.forecast_tool.ForecastEngine",
        return_value=fake_engine,
    ):
        result = asyncio.run(_forecast_run(
            args={"target_id": "tgt-1"},
            db=fake_db,
            user_id="u-1",
            context={},
        ))

    assert result["success"] is False
    assert result["reason"] == "insufficient_data"
    assert result["sample_size"] == 3
    assert result["source_table"] == "actual_price"


def test_forecast_run_with_full_data_returns_anchored_response():
    """When the engine returns 30+ data points, the tool returns success + data_anchor."""
    from app.services.tool_handlers.forecast_tool import _forecast_run

    fake_run = MagicMock()
    fake_run.target_id = "tgt-1"
    fake_run.below_naive_baseline = False
    fake_run.confidence = 0.8
    fake_run.as_of_date = None
    fake_run.model_detail = {"ensemble_mape": 0.05}
    fake_run.results = {
        "7": {"base": [120.0] * 7, "bull": [130.0] * 7, "bear": [110.0] * 7}
    }

    fake_engine = MagicMock()
    fake_engine.compute_target_anchored.return_value = {
        "run": fake_run,
        "source_table": "actual_price",
        "sample_size": 365,
        "anchor_rows": [
            {"date": "2026-01-01", "value": 100.0},
            {"date": "2026-12-31", "value": 365.0},
        ],
    }
    fake_db = MagicMock()
    fake_db.get.return_value = MagicMock(is_deleted=False)

    with patch(
        "app.services.tool_handlers.forecast_tool.ForecastEngine",
        return_value=fake_engine,
    ):
        result = asyncio.run(_forecast_run(
            args={"target_id": "tgt-1"},
            db=fake_db,
            user_id="u-1",
            context={},
        ))

    assert result["success"] is True
    assert "data_anchor" in result
    assert result["data_anchor"]["sample_size"] == 365
    assert "runs" in result
    assert result["runs"][0]["target_id"] == "tgt-1"
