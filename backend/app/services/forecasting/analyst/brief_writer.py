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
        "summary_zh": {"type": "string"},
        "why_price_zh": {"type": "string"},
        "why_action_zh": {"type": "string"},
        "watch_triggers_zh": {"type": "array", "items": {"type": "string"}},
        "risk_zh": {"type": "string"},
    },
    "required": ["summary_zh", "why_price_zh", "why_action_zh",
                 "watch_triggers_zh", "risk_zh"],
}


def build_analyst_prompt(pack: dict) -> str:
    return (
        "你是石化市场分析师。根据下方 EVIDENCE_JSON 撰写该产品的 AI 简评。\n"
        "严格规则:\n"
        "1. 只能使用 EVIDENCE_JSON 中已提供的数字和事实,禁止自行添加任何数字。\n"
        "2. 不要改变决策:decision.action 已由确定性引擎给出,你只能解释它。\n"
        "3. 输出简体中文 JSON,字段:summary_zh(一句话总览), why_price_zh(价格逻辑:"
        "上游传导/趋势/季节/模型驱动), why_action_zh(建议逻辑:为什么是这个操作建议,"
        "信任等级原因), watch_triggers_zh(2-4 条重新评估触发条件,字符串数组), "
        "risk_zh(风险提示)。\n"
        "4. 即使置信度低、建议观望,也必须解释清楚为什么以及应该关注什么。\n\n"
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
