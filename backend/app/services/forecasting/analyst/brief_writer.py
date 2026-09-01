"""LLM analyst brief writer — grounded prose from the evidence pack only."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.services.forecasting.analyst.verifier import verify_brief

logger = logging.getLogger(__name__)

BRIEF_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "market_update_zh": {"type": "string"},
        "price_data_zh": {"type": "string"},
        "upstream_logic_zh": {"type": "string"},
        "supply_demand_zh": {"type": "string"},
        "forecast_zh": {"type": "string"},
        "watch_triggers_zh": {"type": "array", "items": {"type": "string"}},
        "risk_zh": {"type": "string"},
    },
    "required": ["market_update_zh", "price_data_zh", "upstream_logic_zh",
                 "supply_demand_zh", "forecast_zh", "watch_triggers_zh", "risk_zh"],
}


def build_analyst_prompt(pack: dict) -> str:
    is_upstream = pack.get("product_group") == "upstream"
    if is_upstream:
        supply_demand_rule = (
            "   - supply_demand_zh(宏观供需:近30日价格涨跌、价格分位、波动状态、"
            "首要驱动因子、模型间分歧。上游产品无ERP销量/开工率数据,禁止编造交易量)\n"
        )
        upstream_rule = (
            "   - upstream_logic_zh(上游传导:原油为产业链最上游,无上游原料,"
            "写宏观市场情绪、在册事件、波动状态;若有上游原料数据则写原料变化与弹性传导)\n"
        )
    else:
        supply_demand_rule = (
            "   - supply_demand_zh(供需研判:季节性、模型驱动因子、模型间分歧)\n"
        )
        upstream_rule = (
            "   - upstream_logic_zh(上游传导:上游原料变化、弹性传导、与模型方向是否一致)\n"
        )
    return (
        "你是石化市场分析师。根据下方 EVIDENCE_JSON 撰写该产品的 AI 简评,按周报格式分7个板块。\n"
        "严格规则:\n"
        "1. 只能使用 EVIDENCE_JSON 中已提供的数字和事实,禁止自行添加任何数字。\n"
        "2. 不要改变决策:decision.action 已由确定性引擎给出,你只能解释它。\n"
        "3. 输出简体中文 JSON,7个字段对应7个板块:\n"
        "   - market_update_zh(市场动态:市场情报方向、在册事件数、波动状态)\n"
        "   - price_data_zh(价格数据:现价、近三年分位、近7日/30日涨跌、均线位置)\n"
        f"{upstream_rule}"
        f"{supply_demand_rule}"
        "   - forecast_zh(价格预测:必须给出区间[bear–bull]和方向箭头,如 ↘偏弱/↗偏强/→稳定/↔震荡,"
        "可加定性词如易跌难涨;附上涨概率和置信度)\n"
        "   - watch_triggers_zh(2-4 条重新评估触发条件,字符串数组)\n"
        "   - risk_zh(风险提示)\n"
        "4. 即使置信度低、建议观望,也必须解释清楚为什么以及应该关注什么。\n"
        "5. 方向箭头规则:预期变化≥+2%用↗偏强,≤-2%用↘偏弱,|变化|<1.5%且模型分歧大用↔震荡,否则→稳定。\n\n"
        f"EVIDENCE_JSON:\n{json.dumps(pack, ensure_ascii=False, default=str)}"
    )


def write_brief_llm(pack: dict, llm_caller) -> dict | None:
    """Call the LLM and return a verified brief, or None on any failure.

    llm_caller: callable(prompt: str, schema: dict) -> dict
    (matches llm_service.chat_completion_json_sync's call shape).
    """
    try:
        out = llm_caller(build_analyst_prompt(pack), BRIEF_JSON_SCHEMA)
    except Exception as exc:
        logger.warning("[analyst] LLM brief call failed: %s", exc)
        return None
    if not isinstance(out, dict) or not out:
        return None
    violations = verify_brief(out, pack)
    if violations:
        logger.warning("[analyst] brief rejected by verifier: %s", violations)
        return None
    out["source"] = "llm"
    out["day"] = pack.get("day")
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    return out
