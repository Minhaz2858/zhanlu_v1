"""Tests for forecasting.narrative — the AI 分析依据 reasoning builders."""

import pytest

from app.services.forecasting import narrative as fn


# ── compute_trend_stats ─────────────────────────────────────────────────

def _rows(prices):
    return [(f"2026-07-{i + 1:02d}", float(p)) for i, p in enumerate(prices)]


class TestComputeTrendStats:
    def test_empty(self):
        out = fn.compute_trend_stats([])
        assert out["last_price"] is None
        assert out["chg_7d_pct"] is None
        assert out["above_ma30"] is None

    def test_short_series_no_windows(self):
        out = fn.compute_trend_stats(_rows([100, 101, 102]))
        assert out["last_price"] == 102.0
        assert out["chg_7d_pct"] is None      # needs >= 8 points
        assert out["chg_30d_pct"] is None     # needs >= 31 points
        assert out["ma30"] == pytest.approx(101.0)
        assert out["above_ma30"] is True

    def test_7d_change(self):
        prices = [100.0] * 7 + [110.0]        # 8 points: last vs 7-back
        out = fn.compute_trend_stats(_rows(prices))
        assert out["chg_7d_pct"] == pytest.approx(0.10)

    def test_30d_change_and_ma(self):
        prices = [100.0] * 30 + [120.0]       # 31 points
        out = fn.compute_trend_stats(_rows(prices))
        assert out["chg_30d_pct"] == pytest.approx(0.20)
        expected_ma = (100.0 * 29 + 120.0) / 30
        assert out["ma30"] == pytest.approx(expected_ma)
        assert out["above_ma30"] is True

    def test_below_ma(self):
        prices = [100.0] * 30 + [80.0]
        out = fn.compute_trend_stats(_rows(prices))
        assert out["above_ma30"] is False

    def test_none_prices_skipped(self):
        rows = [("2026-07-01", None), ("2026-07-02", 100.0)]
        out = fn.compute_trend_stats(rows)
        assert out["last_price"] == 100.0


# ── compute_uncertainty_stats ───────────────────────────────────────────

class TestComputeUncertaintyStats:
    def test_empty(self):
        out = fn.compute_uncertainty_stats({})
        assert out["spread_pct"] is None

    def test_arrays_uses_last_point(self):
        payload = {
            "base": [100.0, 110.0],
            "bull": [105.0, 121.0],
            "bear": [95.0, 99.0],
        }
        out = fn.compute_uncertainty_stats(payload)
        assert out["base"] == 110.0
        assert out["bull"] == 121.0
        assert out["bear"] == 99.0
        assert out["spread_pct"] == pytest.approx((121.0 - 99.0) / 110.0)

    def test_scalars(self):
        payload = {"base": 100.0, "bull": 110.0, "bear": 90.0}
        out = fn.compute_uncertainty_stats(payload)
        assert out["spread_pct"] == pytest.approx(0.2)

    def test_missing_bear(self):
        out = fn.compute_uncertainty_stats({"base": [100.0], "bull": [110.0]})
        assert out["spread_pct"] is None

    def test_not_a_dict(self):
        out = fn.compute_uncertainty_stats(None)
        assert out["spread_pct"] is None


# ── compute_model_stats ─────────────────────────────────────────────────

class TestComputeModelStats:
    def test_empty(self):
        out = fn.compute_model_stats(None, None)
        assert out["model_count"] == 0
        assert out["beats_naive"] is None

    def test_full(self):
        md = {
            "models_run": ["naive_last", "ets", "arima"],
            "ensemble_mape": 0.08,
            "naive_mape": 0.12,
        }
        out = fn.compute_model_stats(md, False)
        assert out["model_count"] == 3
        assert out["beats_naive"] is True
        assert out["below_naive_baseline"] is False

    def test_worse_than_naive(self):
        md = {"models_run": ["a"], "ensemble_mape": 0.20, "naive_mape": 0.12}
        out = fn.compute_model_stats(md, True)
        assert out["beats_naive"] is False
        assert out["below_naive_baseline"] is True


# ── build_basis_zh ──────────────────────────────────────────────────────

class TestBuildBasisZh:
    def test_empty_inputs(self):
        assert fn.build_basis_zh(trend={}, uncertainty={}, models={}) == ""

    def test_full_sentence(self):
        trend = {"chg_7d_pct": 0.008, "chg_30d_pct": -0.02, "above_ma30": True}
        unc = {"spread_pct": 0.10}
        models = {
            "model_count": 6, "ensemble_mape": 0.082, "naive_mape": 0.121,
            "beats_naive": True, "below_naive_baseline": False,
        }
        s = fn.build_basis_zh(trend=trend, uncertainty=unc, models=models)
        assert "趋势:" in s and "+0.8%" in s and "-2.0%" in s
        assert "均线上方" in s
        assert "6 模型集成" in s and "优于" in s
        assert "不确定性" in s and "适中" in s
        assert s.endswith("。")

    def test_honesty_gate_note(self):
        models = {
            "model_count": 5, "ensemble_mape": None, "naive_mape": None,
            "beats_naive": None, "below_naive_baseline": True,
        }
        s = fn.build_basis_zh(trend={}, uncertainty={}, models=models)
        assert "诚实门控" in s

    def test_spread_levels(self):
        small = fn.build_basis_zh(trend={}, uncertainty={"spread_pct": 0.05}, models={})
        large = fn.build_basis_zh(trend={}, uncertainty={"spread_pct": 0.25}, models={})
        assert "较小" in small
        assert "较大" in large


# ── build_action_logic_zh ───────────────────────────────────────────────

class TestBuildActionLogicZh:
    def test_watch_low_trust(self):
        s = fn.build_action_logic_zh(
            action="watch", confidence="low",
            p_rise=0.62, expected_change_pct=0.023,
            directional_accuracy=0.52, directional_status="no_edge",
            trust_tier="low",
        )
        assert "上涨概率 62%" in s
        assert "可信度等级低" in s
        assert "52% 未达 55%" in s
        assert "升级" in s
        assert "置信度:低" in s

    def test_watch_no_accuracy_data(self):
        s = fn.build_action_logic_zh(
            action="watch", confidence="low",
            p_rise=None, expected_change_pct=None,
            directional_accuracy=None, directional_status=None,
            trust_tier="medium",
        )
        assert "无统计显著优势" in s

    def test_buy(self):
        s = fn.build_action_logic_zh(
            action="buy", confidence="high",
            p_rise=0.75, expected_change_pct=0.04,
            directional_accuracy=0.61, directional_status="edge",
            trust_tier="high",
        )
        assert "满足备货信号" in s
        assert "置信度:高" in s

    def test_sell(self):
        s = fn.build_action_logic_zh(
            action="sell", confidence="medium",
            p_rise=0.25, expected_change_pct=-0.05,
            directional_accuracy=0.60, directional_status="edge",
            trust_tier="medium",
        )
        assert "满足出货信号" in s

    def test_hold(self):
        s = fn.build_action_logic_zh(
            action="hold", confidence="medium",
            p_rise=0.60, expected_change_pct=0.015,
            directional_accuracy=0.58, directional_status="edge",
            trust_tier="medium",
        )
        assert "按需跟进" in s
        assert "升级为备货" in s
