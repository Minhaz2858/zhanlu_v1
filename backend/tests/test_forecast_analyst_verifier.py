"""Brief output verifier — the anti-hallucination gate."""
from app.services.forecasting.analyst.verifier import verify_brief
from tests.test_forecast_analyst_template import _dcpd_pack


def _brief(**over):
    b = {
        "summary_zh": "双环戊二烯现价 6,940 元/吨,预计未来 7 天跌至 6,660 元/吨(-4.0%)。",
        "why_price_zh": "上游裂解碳五近30日-2.8%,按 0.66 弹性传导约 -1.9%。",
        "why_action_zh": "建议观望 —— 历史数据不足,方向准确率 51% 未达 55% 门槛。",
        "watch_triggers_zh": ["方向准确率回升至 55% 以上"],
        "risk_zh": "近期无重大市场事件。",
    }
    b.update(over)
    return b


def test_valid_brief_passes():
    pack = _dcpd_pack()
    # numbers: 6940 (current), 6660 (base), -4.0 (exp*100), 51 (acc*100), 55 threshold
    assert verify_brief(_brief(), pack) == []


def test_hallucinated_price_rejected():
    pack = _dcpd_pack()
    v = verify_brief(_brief(summary_zh="现价 7,200 元/吨,预计涨至 7,500 元/吨(+4.2%)。"), pack)
    assert v != []
    assert any("7,200" in msg or "7,500" in msg for msg in v)


def test_missing_key_rejected():
    b = _brief()
    del b["why_price_zh"]
    assert verify_brief(b, _dcpd_pack()) != []


def test_triggers_must_be_list():
    assert verify_brief(_brief(watch_triggers_zh="not a list"), _dcpd_pack()) != []


def test_small_numbers_exempt():
    # 7 (day), 8 (month), 3 (count) — all ≤12, exempt
    assert verify_brief(_brief(why_price_zh="8 模型集成,7 日维度,3 大因素。"), _dcpd_pack()) == []


def test_unsigned_chinese_prose_accepted():
    # "下跌17.97%" style — magnitude is in pack as negative; must pass.
    pack = {"trend": {"chg_30d_pct": -0.1797}, "day": 7,
            "thresholds": {"trend_window_days": 30}}
    brief = {
        "summary_zh": "现价 100 元。", "why_price_zh": "近30日下跌17.97%。",
        "why_action_zh": "观望。", "watch_triggers_zh": [], "risk_zh": "",
    }
    # 100 not in pack -> replace with pack-free summary to isolate the sign rule
    brief["summary_zh"] = "价格走弱。"
    assert verify_brief(brief, pack) == []


def test_invented_magnitude_still_rejected():
    pack = {"trend": {"chg_30d_pct": -0.1797}, "day": 7}
    brief = {
        "summary_zh": "价格走弱。", "why_price_zh": "近30日下跌 33.5%。",
        "why_action_zh": "观望。", "watch_triggers_zh": [], "risk_zh": "",
    }
    assert verify_brief(brief, pack) != []
