"""Brief output verifier — the anti-hallucination gate."""
from app.services.forecasting.analyst.verifier import verify_brief
from tests.test_forecast_analyst_template import _dcpd_pack


def _brief(**over):
    b = {
        "market_update_zh": "市场动态:市场情报中性,当前波动正常。",
        "price_data_zh": "价格数据:现价 6,940 元/吨。",
        "upstream_logic_zh": "上游裂解碳五近30日-2.8%,按 0.66 弹性传导约 -1.9%。",
        "supply_demand_zh": "供需研判:8模型间分歧偏大。",
        "forecast_zh": "价格预测:预计未来 7 天 6,350–6,950 元/吨,↘ 偏弱,上涨概率 42%。",
        "watch_triggers_zh": ["方向准确率回升至 55% 以上"],
        "risk_zh": "近期无重大市场事件。",
    }
    b.update(over)
    return b


def test_valid_brief_passes():
    pack = _dcpd_pack()
    # numbers: 6940 (current), 6400/6900 (bear/bull), 42 (p_rise*100), 55 threshold
    assert verify_brief(_brief(), pack) == []


def test_hallucinated_price_rejected():
    pack = _dcpd_pack()
    v = verify_brief(_brief(price_data_zh="现价 7,200 元/吨,预计涨至 7,500 元/吨(+4.2%)。"), pack)
    assert v != []
    assert any("7,200" in msg or "7,500" in msg for msg in v)


def test_missing_key_rejected():
    b = _brief()
    del b["upstream_logic_zh"]
    assert verify_brief(b, _dcpd_pack()) != []


def test_triggers_must_be_list():
    assert verify_brief(_brief(watch_triggers_zh="not a list"), _dcpd_pack()) != []


def test_small_numbers_exempt():
    # 7 (day), 8 (models), 3 (count) — all ≤12, exempt
    assert verify_brief(_brief(supply_demand_zh="8 模型集成,7 日维度,3 大因素。"), _dcpd_pack()) == []


def test_unsigned_chinese_prose_accepted():
    # "下跌17.97%" style — magnitude is in pack as negative; must pass.
    pack = {"trend": {"chg_30d_pct": -0.1797}, "day": 7,
            "thresholds": {"trend_window_days": 30}}
    brief = {
        "market_update_zh": "", "price_data_zh": "价格走弱。",
        "upstream_logic_zh": "近30日下跌17.97%。",
        "supply_demand_zh": "", "forecast_zh": "",
        "watch_triggers_zh": [], "risk_zh": "",
    }
    assert verify_brief(brief, pack) == []


def test_invented_magnitude_still_rejected():
    pack = {"trend": {"chg_30d_pct": -0.1797}, "day": 7}
    brief = {
        "market_update_zh": "", "price_data_zh": "价格走弱。",
        "upstream_logic_zh": "近30日下跌 33.5%。",
        "supply_demand_zh": "", "forecast_zh": "",
        "watch_triggers_zh": [], "risk_zh": "",
    }
    assert verify_brief(brief, pack) != []
