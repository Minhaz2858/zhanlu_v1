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
