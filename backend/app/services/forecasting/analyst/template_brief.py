"""Deterministic brief renderer — 7-section structure mirroring human weekly reports.

Sections (matching human report layout):
  市场动态 / 价格数据 / 上游传导 / 供需研判 / 价格预测 / 触发条件 / 风险提示
"""
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

# Directional-signal thresholds sit BELOW the decision engine's _MIN_CHG so
# the qualitative arrow is more sensitive than the buy/sell action gate.
_DIR_STRONG = 0.02      # |chg| >= 2% → 偏强/偏弱
_DIR_RANGE = 0.015      # |chg| < 1.5% with high spread → 震荡
_DIR_SPREAD = 0.04      # spread_pct threshold for 震荡

_INTEL_BIAS_ZH = {"up": "偏多", "down": "偏空", "neutral": "中性"}
_VOL_ZH = {"NORMAL": "正常", "MODERATE": "偏高", "HIGH": "剧烈"}
_CONF_ZH = {"high": "高", "medium": "中", "low": "低"}


def _fmt_price(v):
    return "—" if v is None else f"{v:,.0f}"


def _fmt_pct_signed(v, digits=1):
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _fmt_pct_plain(v, digits=0):
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _directional_signal(p: dict) -> tuple[str, str]:
    """Returns (arrow, label_zh) from expected_change_pct + spread_pct."""
    chg = p.get("expected_change_pct")
    if chg is None:
        chg = p.get("implied_change_pct")
    spread = p.get("spread_pct") or 0
    if chg is None:
        return ("?", "方向不明")
    abs_chg = abs(chg)
    if abs_chg < _DIR_RANGE and spread > _DIR_SPREAD:
        return ("↔", "震荡")
    if chg >= _DIR_STRONG:
        return ("↗", "偏强")
    if chg <= -_DIR_STRONG:
        return ("↘", "偏弱")
    return ("→", "稳定")


def _qualitative_tail(p: dict) -> str:
    """Qualitative phrase the human reports use (易跌难涨 / 易涨难跌)."""
    chg = p.get("expected_change_pct")
    p_rise = p.get("p_rise")
    if chg is not None and p_rise is not None:
        if chg < 0 and p_rise < 0.45:
            return "易跌难涨"
        if chg > 0 and p_rise > 0.55:
            return "易涨难跌"
    return ""


# ── Section renderers (pure functions) ──────────────────────────────

def _market_update(p: dict) -> str:
    """市场动态 — intel events + volatility regime."""
    intel = p.get("intelligence") or {}
    count = intel.get("event_count", 0)
    bias = intel.get("bias", "neutral")
    bias_zh = _INTEL_BIAS_ZH.get(bias, "中性")
    vol = (p.get("policy") or {}).get("volatility_regime")
    vol_zh = _VOL_ZH.get(vol, "") if vol else ""
    bits = []
    summary = intel.get("summary")
    if summary:
        bits.append(summary)
    bits.append(f"市场情报{bias_zh}")
    if count:
        bits.append(f"在册事件 {count} 条")
    if vol_zh:
        bits.append(f"当前波动{vol_zh}")
    return "市场动态:" + ",".join(bits) + "。"


def _price_data(p: dict) -> str:
    """价格数据 — current + percentile + 7d/30d trend."""
    cur = p.get("current_price")
    if cur is None:
        return "价格数据:暂无现价数据。"
    bits = [f"现价 {_fmt_price(cur)} 元/吨"]
    pctile = p.get("price_percentile")
    if pctile is not None:
        bits.append(f"处于近三年约 {pctile:.0f}% 分位")
    t = p.get("trend") or {}
    tb = []
    if t.get("chg_7d_pct") is not None:
        tb.append(f"近7日{_fmt_pct_signed(t['chg_7d_pct'])}")
    if t.get("chg_30d_pct") is not None:
        tb.append(f"近30日{_fmt_pct_signed(t['chg_30d_pct'])}")
    if t.get("above_ma30") is not None:
        tb.append("位于30日均线上方" if t["above_ma30"] else "位于30日均线下方")
    if tb:
        bits.append(",".join(tb))
    return "价格数据:" + ";".join(bits) + "。"


def _upstream_logic(p: dict) -> str:
    """上游传导 — causal chain (upstream Δ% × elasticity → implied)."""
    up = (p.get("upstream") or [{}])[0]
    causal = p.get("causal") or {}
    if up.get("chg_30d_pct") is None and causal.get("elasticity") is None:
        return ""
    bits = []
    if up.get("chg_30d_pct") is not None and up.get("name_zh"):
        seg = f"上游{up['name_zh']}近30日{up['chg_30d_pct']:+.1f}%"
        if causal.get("elasticity") is not None and causal.get("implied_pct") is not None:
            seg += f",按 {causal['elasticity']:.2f} 弹性传导约 {causal['implied_pct']:+.1f}%"
            if p.get("expected_change_pct") is not None:
                model_pct = p["expected_change_pct"] * 100
                seg += (",与模型预测方向一致" if not causal.get("divergent")
                        else f",而模型预测 {model_pct:+.1f}%,方向背离")
        bits.append(seg)
    if not bits:
        return ""
    return "上游传导:" + ";".join(bits) + "。"


def _supply_demand(p: dict) -> str:
    """供需研判 — seasonal + model drivers + model agreement + demand/supplier signals (Wave 1)."""
    bits = []
    seas = p.get("seasonal") or {}
    if seas.get("label_zh"):
        bits.append(f"当前为{seas['month']}月{seas['label_zh']}"
                    f"(季节调整 {seas['adj_pct']:+.1f}%)")
    drivers = p.get("drivers") or []
    if drivers:
        bits.append(f"模型首要驱动因子:{drivers[0]['feature']}"
                    f"(权重 {drivers[0]['weight'] * 100:.0f}%)")
    ag = (p.get("models") or {}).get("agreement")
    if ag and ag.get("spread_pct") is not None and ag["spread_pct"] > 0.02:
        bits.append(f"{ag['n_models']} 模型间分歧偏大(区间 "
                    f"{_fmt_price(ag['min'])}–{_fmt_price(ag['max'])})")

    # Wave 1: demand signal (ERP volume)
    demand = p.get("demand") or {}
    if demand.get("has_sufficient_data"):
        trend_map = {"up": "需求上行", "down": "需求收缩", "flat": "需求平稳"}
        trend_text = trend_map.get(demand.get("demand_trend", ""), "")
        if demand.get("rolling_4wk_vol") is not None:
            bits.append(f"近4周销量均值{int(demand['rolling_4wk_vol'])}吨，{trend_text}")
        yoy = demand.get("yoy_change_pct")
        if yoy is not None and abs(yoy) > 0.01:
            yoy_pct = abs(yoy) * 100
            direction = f"同比+{yoy_pct:.0f}%" if yoy >= 0 else f"同比-{yoy_pct:.0f}%"
            bits.append(direction)
        div = demand.get("vol_price_divergence")
        if div is not None and abs(div) > 0.1:
            bits.append("量价背离" if div > 0 else "量价收敛")

    # Wave 1: supplier ladder signal
    supplier = p.get("supplier_ladder") or {}
    if supplier.get("has_data") and supplier.get("avg_spread") is not None:
        bits.append(f"供应商价差均值{int(supplier['avg_spread'])}，"
                    f"趋势{supplier.get('spread_trend', '平稳')}"
                    f"（近{supplier.get('recent_days', 30)}天）")

    # Wave 3 T3.5: downstream utilization (开工率)
    op = p.get("downstream_utilization") or {}
    if op.get("has_sufficient_data"):
        rate = op.get("rolling_4wk_op_rate")
        if rate is not None:
            regime_map = {"tight": "开工率偏高", "normal": "开工率正常",
                          "loose": "开工率偏低"}
            regime_text = regime_map.get(op.get("utilization_regime", ""), "")
            bits.append(f"下游开工率{rate:.1f}%（{regime_text}）")
        yoy = op.get("yoy_change_pct")
        if yoy is not None and abs(yoy) > 0.5:
            sign = "+" if yoy >= 0 else ""
            bits.append(f"开工率同比{sign}{yoy:.1f}%")

    # Wave 3 T3.5: inventory pressure (库存)
    inv = p.get("inventory_pressure") or {}
    if inv.get("has_sufficient_data"):
        change = inv.get("inventory_4wk_change_pct")
        pressure_map = {"high": "库存高位", "low": "库存低位",
                        "normal": "库存正常"}
        pressure_text = pressure_map.get(inv.get("inventory_pressure", ""), "")
        if change is not None and abs(change) > 0.5:
            sign = "+" if change >= 0 else ""
            bits.append(f"近4周库存{sign}{change:.1f}%（{pressure_text}）")

    if not bits:
        return ""
    return "供需研判:" + ";".join(bits) + "。"


def _is_upstream(p: dict) -> bool:
    return p.get("product_group") == "upstream"


def _macro_supply_demand(p: dict) -> str:
    """宏观供需 — upstream products have no ERP sales volume, so render a macro
    supply/demand narrative from price position, trend, volatility and drivers."""
    bits: list[str] = []
    t = p.get("trend") or {}
    if t.get("chg_30d_pct") is not None:
        bits.append(f"近30日价格{_fmt_pct_signed(t['chg_30d_pct'])}")
    pctile = p.get("price_percentile")
    if pctile is not None:
        if pctile >= 70:
            bits.append(f"价格处近三年高位(约 {pctile:.0f}% 分位)")
        elif pctile <= 30:
            bits.append(f"价格处近三年低位(约 {pctile:.0f}% 分位)")
        else:
            bits.append(f"价格处近三年中位(约 {pctile:.0f}% 分位)")
    vol = (p.get("policy") or {}).get("volatility_regime")
    vol_zh = _VOL_ZH.get(vol, "") if vol else ""
    if vol_zh:
        bits.append(f"波动{vol_zh}")
    drivers = p.get("drivers") or []
    if drivers:
        bits.append(f"首要驱动因子:{drivers[0]['feature']}"
                    f"(权重 {drivers[0]['weight'] * 100:.0f}%)")
    ag = (p.get("models") or {}).get("agreement")
    if ag and ag.get("spread_pct") is not None and ag["spread_pct"] > 0.02:
        bits.append(f"{ag['n_models']} 模型间分歧偏大(区间 "
                    f"{_fmt_price(ag['min'])}–{_fmt_price(ag['max'])})")
    intel = p.get("intelligence") or {}
    if intel.get("event_count"):
        bits.append(f"在册事件 {intel['event_count']} 条")
    if not bits:
        return "宏观供需:暂无足够宏观数据,建议结合国际市场价格与库存周期研判。"
    return "宏观供需:" + ";".join(bits) + "。"


def _upstream_macro_context(p: dict) -> str:
    """上游传导 — when there is no usable upstream parent (the product sits at
    the top of the chain, or upstream data missing), render a global macro
    context narrative instead of a causal elasticity chain that would otherwise
    be empty."""
    intel = p.get("intelligence") or {}
    bias = intel.get("bias", "neutral")
    bias_zh = _INTEL_BIAS_ZH.get(bias, "中性")
    vol = (p.get("policy") or {}).get("volatility_regime")
    vol_zh = _VOL_ZH.get(vol, "") if vol else ""
    upstream = p.get("upstream") or []
    if upstream:
        head = f"上游{upstream[0].get('name_zh', '')}数据暂缺"
    else:
        head = f"{p.get('name_zh', '')}为产业链最上游,无上游原料传导"
    bits = [head]
    bits.append(f"宏观市场情绪{bias_zh}")
    if intel.get("event_count"):
        bits.append(f"在册宏观事件 {intel['event_count']} 条")
    if vol_zh:
        bits.append(f"当前波动{vol_zh}")
    t = p.get("trend") or {}
    if t.get("chg_30d_pct") is not None:
        bits.append(f"近30日价格{_fmt_pct_signed(t['chg_30d_pct'])}")
    return "上游传导:" + ",".join(bits) + "。"


def _forecast(p: dict) -> str:
    """价格预测 — range [bear,bull] + directional signal + confidence + action advice."""
    # P0.4: distinct message for below_naive vs truly sparse data
    below_naive = "below_naive_baseline" in (p.get("reason_codes") or [])
    if below_naive:
        return (
            f"价格预测:{p.get('name_zh', '')}当前模型弱于简单基准,"
            f"展示保守基线,建议观望。"
        )
    if p.get("forecast_base") is None or p.get("current_price") is None:
        return f"价格预测:{p.get('name_zh', '')}当前预测数据不足,建议观望。"
    arrow, label = _directional_signal(p)
    bear = p.get("bear")
    bull = p.get("bull")
    parts = [f"预计未来 {p.get('day', 7)} 天主流价格"]
    if bear is not None and bull is not None:
        parts.append(f" {_fmt_price(bear)}–{_fmt_price(bull)} 元/吨")
    else:
        parts.append(f"约 {_fmt_price(p['forecast_base'])} 元/吨")
    parts.append(f",{arrow} {label}")
    qual = _qualitative_tail(p)
    if qual:
        parts.append(f"({qual})")
    if p.get("p_rise") is not None:
        parts.append(f",上涨概率 {_fmt_pct_plain(p['p_rise'])}")
    conf = (p.get("decision") or {}).get("confidence", "low")
    parts.append(f"(置信度:{_CONF_ZH.get(conf, '低')})")

    # Action advice + reasoning — embedded like human reports' closing recommendation
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
        parts.append(f"。{advice} —— {why},不构成可操作信号")
    elif action in ("buy", "sell"):
        if action == "buy":
            cond = (f"上涨概率≥{_fmt_pct_plain(_BUY_P)} 且预期涨幅≥"
                    f"{_fmt_pct_plain(_MIN_CHG)} 且方向准确率达标")
        else:
            cond = (f"上涨概率≤{_fmt_pct_plain(_SELL_P)} 且预期跌幅≥"
                    f"{_fmt_pct_plain(_MIN_CHG)} 且方向准确率达标")
        tail = f"({';'.join(reasons)})" if reasons else ""
        parts.append(f"。{advice} —— 满足条件:{cond}{tail}")
    else:
        parts.append(f"。{advice}")

    return "价格预测:" + "".join(parts) + "。"


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

    # Wave 3 T3.5: import pressure (进口/竞争对手价格)
    imp = p.get("import_pressure") or {}
    if imp.get("has_sufficient_data") and imp.get("import_window_open"):
        gap = imp.get("import_parity_gap")
        if gap is not None:
            risks.append(f"进口窗口打开(国内外价差{gap * 100:.1f}%),"
                         f"价格上行空间受限")

    return "风险提示:" + ";".join(risks) + "。" if risks else ""


# ── Field order for narrative concatenation ─────────────────────────
# (used by decision_board_service + weekly_report generator)
SECTION_TITLES = [
    ("market_update_zh", "【市场动态】"),
    ("price_data_zh", "【价格数据】"),
    ("upstream_logic_zh", "【上游传导】"),
    ("supply_demand_zh", "【供需研判】"),
    ("forecast_zh", "【价格预测】"),
]


def render_template_brief(pack: dict) -> dict:
    is_up = _is_upstream(pack)
    # A top-of-chain product has an empty upstream list, so `_upstream_logic`
    # would return "" — render macro context instead. Products with upstream
    # parents but no ERP demand volume swap supply/demand.
    upstream_has_parent = bool((pack.get("upstream") or [{}])[0].get("chg_30d_pct") is not None)
    return {
        "market_update_zh": _market_update(pack),
        "price_data_zh": _price_data(pack),
        "upstream_logic_zh": (
            _upstream_macro_context(pack) if (is_up and not upstream_has_parent)
            else _upstream_logic(pack)
        ),
        "supply_demand_zh": (
            _macro_supply_demand(pack) if is_up else _supply_demand(pack)
        ),
        "forecast_zh": _forecast(pack),
        "watch_triggers_zh": _triggers(pack),
        "risk_zh": _risk(pack),
        "source": "template",
        "day": pack.get("day"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
