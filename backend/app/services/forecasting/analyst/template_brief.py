"""Deterministic brief renderer — enriched fallback for the LLM writer."""
from __future__ import annotations

from datetime import datetime, timezone

_ACTION_ADVICE_ZH = {
    "buy": "建议提前备货,锁定当前价格",
    "sell": "建议抓紧出货,兑现当前高价",
    "hold": "建议按需跟进,不追涨不囤货",
    "watch": "建议保持观望,等待更明确的信号",
}
_REASON_CODE_ZH = {
    "sparse_data": "历史数据不足",
    "below_naive_baseline": "模型回测弱于朴素基线",
    "weekly_cadence": "周频交易产品,日度精度有限",
    "model_skill_high": "模型回测持续跑赢基准",
    "model_skill_medium": "模型具备一定预测能力",
}
_BUY_P, _SELL_P, _MIN_CHG, _EDGE = 0.70, 0.30, 0.03, 0.55


def _fmt_price(v):
    return "—" if v is None else f"{v:,.0f}"


def _fmt_pct_signed(v, digits=1):
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _fmt_pct_plain(v, digits=0):
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _summary(p: dict) -> str:
    if p.get("current_price") is None or p.get("forecast_base") is None:
        return f"{p['name_zh']}当前预测数据不足,建议观望。"
    # Direction: prefer the point forecast vs current price; fall back to the
    # probability model's expected change.
    if p.get("implied_change_pct") is not None:
        move = "涨至" if p["implied_change_pct"] >= 0 else "跌至"
    else:
        move = "涨至" if (p.get("expected_change_pct") or 0) >= 0 else "跌至"
    change = p.get("expected_change_pct")
    if change is None:
        change = p.get("implied_change_pct")
    s = f"{p['name_zh']}现价 {_fmt_price(p['current_price'])} 元/吨"
    if p.get("price_percentile") is not None:
        s += f",处于近三年约 {p['price_percentile']:.0f}% 分位"
    s += f"。预计未来 {p['day']} 天{move} {_fmt_price(p['forecast_base'])} 元/吨"
    if change is not None:
        s += f"({_fmt_pct_signed(change)})"
    if p.get("p_rise") is not None:
        s += f",上涨概率 {_fmt_pct_plain(p['p_rise'])}"
    return s + "。"


def _why_price(p: dict) -> str:
    if p.get("current_price") is None:
        return ""
    bits: list[str] = []
    up = (p.get("upstream") or [{}])[0]
    causal = p.get("causal") or {}
    if up.get("chg_30d_pct") is not None:
        seg = f"上游{up['name_zh']}近30日{up['chg_30d_pct']:+.1f}%"
        if causal.get("elasticity") is not None and causal.get("implied_pct") is not None:
            seg += (f",按 {causal['elasticity']:.2f} 弹性传导约 "
                    f"{causal['implied_pct']:+.1f}%")
            if p.get("expected_change_pct") is not None:
                model_pct = p["expected_change_pct"] * 100
                seg += (",与模型预测方向一致" if not causal.get("divergent")
                        else f",而模型预测 {model_pct:+.1f}%,方向背离,需警惕")
        bits.append(seg)
    t = p.get("trend") or {}
    tb = []
    if t.get("chg_7d_pct") is not None:
        tb.append(f"近7日{_fmt_pct_signed(t['chg_7d_pct'])}")
    if t.get("chg_30d_pct") is not None:
        tb.append(f"近30日{_fmt_pct_signed(t['chg_30d_pct'])}")
    if t.get("above_ma30") is not None:
        tb.append("现价位于30日均线上方" if t["above_ma30"] else "现价位于30日均线下方")
    if tb:
        bits.append("趋势:" + ",".join(tb))
    if (p.get("seasonal") or {}).get("label_zh"):
        bits.append(f"当前为{p['seasonal']['month']}月{p['seasonal']['label_zh']}"
                    f"(季节调整 {p['seasonal']['adj_pct']:+.1f}%)")
    drivers = p.get("drivers") or []
    if drivers:
        bits.append(f"模型首要驱动因子:{drivers[0]['feature']}"
                    f"(权重 {drivers[0]['weight'] * 100:.0f}%)")
    ag = (p.get("models") or {}).get("agreement")
    if ag and ag.get("spread_pct") is not None and ag["spread_pct"] > 0.02:
        bits.append(f"{ag['n_models']} 模型间分歧偏大(区间 "
                    f"{_fmt_price(ag['min'])}–{_fmt_price(ag['max'])})")
    if not bits:
        return "" if p.get("current_price") is None else "暂无可归因的价格驱动因素。"
    return "价格逻辑:" + ";".join(bits) + "。"


def _why_action(p: dict) -> str:
    action = (p.get("decision") or {}).get("action", "watch")
    advice = _ACTION_ADVICE_ZH.get(action, _ACTION_ADVICE_ZH["watch"])
    reasons = [_REASON_CODE_ZH[c] for c in (p.get("trust") or {}).get("reason_codes", [])
               if c in _REASON_CODE_ZH]
    dire = p.get("directional") or {}
    if action == "watch":
        if dire.get("accuracy") is not None and dire.get("status") != "edge":
            reasons.append(f"方向准确率 {_fmt_pct_plain(dire['accuracy'])} 未达 "
                           f"{_fmt_pct_plain(_EDGE)} 显著门槛")
        why = "、".join(reasons) if reasons else "信号不明确"
        return (f"{advice} —— {why},不构成可操作信号。"
                f"(置信度:{(p.get('decision') or {}).get('confidence', 'low')})")
    if action == "buy":
        cond = f"上涨概率≥{_fmt_pct_plain(_BUY_P)} 且预期涨幅≥{_fmt_pct_plain(_MIN_CHG)} 且方向准确率达标"
    elif action == "sell":
        cond = f"上涨概率≤{_fmt_pct_plain(_SELL_P)} 且预期跌幅≥{_fmt_pct_plain(_MIN_CHG)} 且方向准确率达标"
    else:
        cond = "有方向优势但预期波动未达买卖阈值"
    tail = f"({';'.join(reasons)})" if reasons else ""
    return f"{advice} —— 满足条件:{cond}{tail}。"


def _triggers(p: dict) -> list[str]:
    out = [f"方向准确率回升至 {_fmt_pct_plain(_EDGE)} 以上且概率突破买卖阈值时,重新评估为可操作信号"]
    causal = p.get("causal") or {}
    up = (p.get("upstream") or [{}])[0]
    if causal.get("implied_pct") is not None and causal["implied_pct"] < 0 and up.get("name_zh"):
        out.append(f"上游{up['name_zh']}止跌企稳,将缓解成本端下压")
    if p.get("expected_change_pct") is not None and p["expected_change_pct"] < 0:
        out.append(f"若上涨概率突破 {_fmt_pct_plain(_BUY_P)} 且预期转正,升级为备货建议")
    else:
        out.append(f"若上涨概率跌破 {_fmt_pct_plain(_SELL_P)} 且预期跌幅超过 "
                   f"{_fmt_pct_plain(_MIN_CHG)},升级为出货建议")
    if (p.get("seasonal") or {}).get("label_zh") == "传统需求淡季":
        out.append("淡季窗口结束后关注需求恢复节奏")
    return out[:4]


def _risk(p: dict) -> str:
    risks: list[str] = []
    if (p.get("causal") or {}).get("divergent"):
        risks.append("上游传导与模型方向背离,预测不确定性升高")
    ag = (p.get("models") or {}).get("agreement")
    if ag and ag.get("spread_pct") is not None and ag["spread_pct"] > 0.05:
        risks.append("模型间分歧显著,点预测参考价值有限")
    if (p.get("policy") or {}).get("volatility_regime") == "HIGH":
        risks.append("当前波动剧烈,注意仓位风险")
    if (p.get("intelligence") or {}).get("event_count", 0) == 0:
        risks.append("近期无重大市场事件,价格主要由供需基本面驱动")
    return "风险提示:" + ";".join(risks) + "。" if risks else ""


def render_template_brief(pack: dict) -> dict:
    return {
        "summary_zh": _summary(pack),
        "why_price_zh": _why_price(pack),
        "why_action_zh": _why_action(pack),
        "watch_triggers_zh": _triggers(pack),
        "risk_zh": _risk(pack),
        "source": "template",
        "day": pack.get("day"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
