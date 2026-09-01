"""Tests for accuracy tracking + adaptive weights."""
from __future__ import annotations

import pytest

from app.services.forecasting.accuracy_tracker import (
    compute_realized_error,
    adaptive_weights,
)


class TestComputeRealizedError:
    def test_no_actuals_returns_none(self):
        result = compute_realized_error([5000.0], [None], None)
        assert result["mape"] is None

    def test_precise_match_gives_zero_mape(self):
        import pandas as pd
        dates = [pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02")]
        actual = pd.Series([5000.0, 5100.0], index=dates)
        result = compute_realized_error([5000.0, 5100.0], dates, actual)
        assert result["mape"] == 0.0
        assert result["n_matched"] == 2

    def test_10_percent_error_gives_10_mape(self):
        import pandas as pd
        dates = [pd.Timestamp("2026-07-01")]
        actual = pd.Series([100.0], index=dates)
        result = compute_realized_error([110.0], dates, actual)
        assert result["mape"] == pytest.approx(10.0)

    def test_signed_error_positive_when_overpredict(self):
        """signed_error should be (predicted-actual)/actual, positive on overprediction."""
        import pandas as pd
        dates = [pd.Timestamp("2026-07-01")]
        actual = pd.Series([100.0], index=dates)
        result = compute_realized_error([110.0], dates, actual)  # 10% over
        assert result["signed_error"] == pytest.approx(0.10)

    def test_signed_error_negative_when_underpredict(self):
        """signed_error should be negative when forecast is below actual."""
        import pandas as pd
        dates = [pd.Timestamp("2026-07-01")]
        actual = pd.Series([100.0], index=dates)
        result = compute_realized_error([90.0], dates, actual)  # 10% under
        assert result["signed_error"] == pytest.approx(-0.10)

    def test_signed_error_none_with_no_match(self):
        """signed_error should be None when no actuals match."""
        result = compute_realized_error([5000.0], [None], None)
        assert result["signed_error"] is None

    def test_mae_rmse_simple(self):
        """MAE = mean(|pred - actual|), RMSE = sqrt(mean((pred-actual)^2))."""
        import pandas as pd
        dates = [pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02")]
        actual = pd.Series([100.0, 200.0], index=dates)
        # pred=110, actual=100 => error=10; pred=190, actual=200 => error=10
        result = compute_realized_error([110.0, 190.0], dates, actual)
        # MAE = (10 + 10) / 2 = 10.0
        assert result["mae"] == pytest.approx(10.0)
        # RMSE = sqrt((100 + 100) / 2) = sqrt(100) = 10.0
        assert result["rmse"] == pytest.approx(10.0)
        # MAPE = (10/100 + 10/200) / 2 * 100 = (0.10 + 0.05) / 2 * 100 = 7.5
        assert result["mape"] == pytest.approx(7.5)

    def test_mae_rmse_perfect_match(self):
        """MAE and RMSE should be 0 for perfect predictions."""
        import pandas as pd
        dates = [pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02")]
        actual = pd.Series([100.0, 200.0], index=dates)
        result = compute_realized_error([100.0, 200.0], dates, actual)
        assert result["mae"] == 0.0
        assert result["rmse"] == 0.0
        assert result["mape"] == 0.0

    def test_mae_rmse_none_with_no_actuals(self):
        """mae and rmse should be None when no actuals are available."""
        result = compute_realized_error([5000.0], [None], None)
        assert result["mae"] is None
        assert result["rmse"] is None


class TestAdaptiveWeights:
    def test_cold_start_returns_backtest_weights(self):
        bw = {"model_a": 0.5, "model_b": 0.5}
        result = adaptive_weights(bw, realized_weights=None)
        assert result == bw

    def test_blend_70_30_split(self):
        bw = {"model_a": 0.5, "model_b": 0.5}
        rw = {"model_a": 0.8, "model_b": 0.2}
        result = adaptive_weights(bw, rw, realized_weight_factor=0.3)
        # model_a: 0.7*0.5 + 0.3*0.8 = 0.59
        # model_b: 0.7*0.5 + 0.3*0.2 = 0.41
        assert result["model_a"] == pytest.approx(0.59)
        assert result["model_b"] == pytest.approx(0.41)

    def test_weights_sum_to_one(self):
        bw = {"model_a": 0.3, "model_b": 0.7}
        rw = {"model_a": 0.6, "model_b": 0.4}
        result = adaptive_weights(bw, rw, realized_weight_factor=0.3)
        total = sum(result.values())
        assert total == pytest.approx(1.0)

    def test_extra_model_not_in_backtest_ignored(self):
        bw = {"model_a": 1.0}
        rw = {"model_a": 0.6, "model_c": 0.4}
        result = adaptive_weights(bw, rw, realized_weight_factor=0.3)
        assert "model_c" not in result
