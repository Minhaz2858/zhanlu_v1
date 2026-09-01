"""LLM brief writer — prompt contract + mocked LLM."""
from app.services.forecasting.analyst.brief_writer import (
    BRIEF_JSON_SCHEMA, build_analyst_prompt, write_brief_llm,
)
from tests.test_forecast_analyst_template import _dcpd_pack


def test_prompt_contains_evidence_and_contract():
    prompt = build_analyst_prompt(_dcpd_pack())
    assert "只能使用" in prompt or "ONLY" in prompt.upper()
    assert "6660" in prompt or "6,660" in prompt   # forecast base in evidence
    assert "双环戊二烯" in prompt
    assert "JSON" in prompt
    assert "forecast_zh" in prompt                  # 7-section contract


def test_writer_success_marks_llm_source():
    pack = _dcpd_pack()
    good = {
        "market_update_zh": "市场动态:市场情报中性。",
        "price_data_zh": "价格数据:现价 6,940 元/吨。",
        "upstream_logic_zh": "上游裂解碳五近30日下跌,按 0.66 弹性传导。",
        "supply_demand_zh": "供需研判:8模型分歧偏大。",
        "forecast_zh": "价格预测:预计 7 日 6,350–6,950 元/吨,↘ 偏弱,上涨概率 42%。",
        "watch_triggers_zh": ["准确率回升至 55% 以上再评估"],
        "risk_zh": "近期无重大市场事件。",
    }
    out = write_brief_llm(pack, llm_caller=lambda prompt, schema: dict(good))
    assert out is not None
    assert out["source"] == "llm"
    assert out["forecast_zh"] == good["forecast_zh"]


def test_writer_returns_none_on_empty_llm():
    assert write_brief_llm(_dcpd_pack(), llm_caller=lambda p, s: {}) is None


def test_writer_returns_none_on_hallucination():
    pack = _dcpd_pack()
    bad = {"market_update_zh": "现价 9,999 元/吨,暴涨 50%。", "price_data_zh": "x",
           "upstream_logic_zh": "y", "supply_demand_zh": "z", "forecast_zh": "w",
           "watch_triggers_zh": [], "risk_zh": "v"}
    assert write_brief_llm(pack, llm_caller=lambda p, s: dict(bad)) is None
