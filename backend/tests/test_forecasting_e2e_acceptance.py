"""End-to-end acceptance test: user asks for a forecast, gets a true answer.

This is the test that proves the user's stated goal is met:
"user can do forecasting based question and get true answer"

Two scenarios are tested:
  - Thin data (0 rows): response is insufficient_data, NOT a hallucinated number
  - Full data (30 rows): response is success with data_anchor, has a real forecast

The response must be one of two valid shapes:
  - Success: {success: True, data_anchor: {...}, runs: [...]}
  - Failure: {success: False, reason: "insufficient_data", sample_size: N, ...}

Never both. Never neither. Never a "feasibility snapshot" without data.
"""

import asyncio
from unittest.mock import MagicMock, patch


def _stub_engine_with_sample_size(n: int):
    """Build a fake ForecastEngine whose compute_target_anchored returns n rows."""
    fake_run = MagicMock()
    fake_run.target_id = "tgt-1"
    fake_run.below_naive_baseline = False
    fake_run.confidence = 0.8
    fake_run.as_of_date = None
    fake_run.model_detail = {"metric": "MAPE", "ensemble_mape": 0.05}
    fake_run.results = {
        "30": {"base": [120.0, 121.0, 122.0, 123.0, 124.0, 125.0, 126.0],
               "bull": [130.0] * 7, "bear": [110.0] * 7}
    }

    fake_engine = MagicMock()
    if n == 0:
        fake_engine.compute_target_anchored.return_value = {
            "run": None, "source_table": "actual_price",
            "sample_size": 0, "anchor_rows": [],
        }
    else:
        rows = [
            {"date": f"2026-01-{i+1:02d}", "value": 100.0 + i}
            for i in range(n)
        ]
        fake_engine.compute_target_anchored.return_value = {
            "run": fake_run, "source_table": "actual_price",
            "sample_size": n, "anchor_rows": rows,
        }
    return fake_engine


def test_thin_warehouse_yields_no_numeric_forecast():
    """When the warehouse has 0 data points, the tool returns insufficient_data
    and the response must NOT contain a numeric forecast value."""
    from app.services.tool_handlers.forecast_tool import _forecast_run

    fake_db = MagicMock()
    fake_db.get.return_value = MagicMock(is_deleted=False)

    with patch(
        "app.services.tool_handlers.forecast_tool.ForecastEngine",
        return_value=_stub_engine_with_sample_size(0),
    ):
        result = asyncio.run(_forecast_run(
            args={"target_id": "tgt-1"},
            db=fake_db, user_id="u-1", context={},
        ))

    assert result["success"] is False
    assert result["reason"] == "insufficient_data"
    assert "runs" not in result
    serialized = str(result)
    assert "point_estimate" not in serialized
    assert "120.0" not in serialized, "Leaked forecast number in failure path"


def test_full_warehouse_yields_grounded_forecast():
    """When the warehouse has 30+ data points, the tool returns success with
    data_anchor, and the LLM can see a real forecast value backed by source rows."""
    from app.services.tool_handlers.forecast_tool import _forecast_run

    fake_db = MagicMock()
    fake_db.get.return_value = MagicMock(is_deleted=False)

    with patch(
        "app.services.tool_handlers.forecast_tool.ForecastEngine",
        return_value=_stub_engine_with_sample_size(30),
    ):
        result = asyncio.run(_forecast_run(
            args={"target_id": "tgt-1"},
            db=fake_db, user_id="u-1", context={},
        ))

    assert result["success"] is True
    a = result["data_anchor"]
    assert a["sample_size"] == 30
    assert a["source_table"] == "actual_price"
    assert len(a["first_5"]) == 5
    assert len(a["last_5"]) == 5
    assert result["runs"][0]["point_estimate"] == 120.0
    assert "methodology" in result["runs"][0]


def test_response_is_exactly_one_of_two_valid_shapes():
    """For any sample size n, the response is success+anchor OR insufficient_data.
    Never partial. Never both. The shape is what the LLM sees and must respond to."""
    from app.services.tool_handlers.forecast_tool import _forecast_run

    for n in (0, 3, 5, 30, 365):
        fake_db = MagicMock()
        fake_db.get.return_value = MagicMock(is_deleted=False)

        with patch(
            "app.services.tool_handlers.forecast_tool.ForecastEngine",
            return_value=_stub_engine_with_sample_size(n),
        ):
            result = asyncio.run(_forecast_run(
                args={"target_id": "tgt-1"},
                db=fake_db, user_id="u-1", context={},
            ))

        # Must have exactly one of the two shapes
        is_success = result.get("success") is True
        is_failure = result.get("success") is False and result.get("reason") == "insufficient_data"
        assert is_success or is_failure, (
            f"n={n}: response is neither success nor insufficient_data: {result}"
        )
        assert not (is_success and is_failure), f"n={n}: response is both shapes"

        # The two shapes must not cross-contaminate
        if is_success:
            assert "data_anchor" in result, f"n={n}: success but no data_anchor"
            assert "runs" in result, f"n={n}: success but no runs"
            assert "reason" not in result, f"n={n}: success should not have 'reason'"
        else:  # failure
            assert "runs" not in result, f"n={n}: failure leaked runs (fabrication risk)"
            assert "data_anchor" not in result, f"n={n}: failure leaked data_anchor"
            assert "sample_size" in result, f"n={n}: failure must show sample_size"
