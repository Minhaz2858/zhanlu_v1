"""Decision board service — plain-language weekly decision summaries.

Composes market_dashboard_service.get_market_snapshot() (current prices)
with get_forecast_base_batch_v2() (forecast + Phase D/E overlays) into one
per-product bundle with Chinese plain-language narratives for the
/market-dashboard/:section Decision Board UI.

Narrative builders are pure functions (no I/O) for unit testing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.models.market_dashboard import PRODUCT_GROUP_MAP, PRODUCT_LABELS
from app.schemas.market_dashboard import (
    AnalystBriefMeta, DecisionBoardProduct, DecisionBoardResponse,
    DecisionRoiMeta, DriverAttributionMeta, SkuBrief,
)
from app.services import market_dashboard_service as mds

ACTION_LABELS_ZH = {
    "buy": "建议备货",
    "sell": "建议出货",
    "hold": "按需跟进",
    "watch": "建议观望",
}

TRUST_LABELS_ZH = {
    "high": "高可信",
    "medium": "中等可信",
    "directional": "方向性参考",
    "low": "低可信",
}

_CONFIDENCE_ZH = {"high": "高", "medium": "中", "low": "低"}
_VOLATILITY_ZH = {"NORMAL": "正常", "MODERATE": "偏高", "HIGH": "剧烈"}
_INTEL_BIAS_ZH = {"up": "偏多", "down": "偏空", "neutral": "中性"}

_ACTION_ADVICE_ZH = {
    "buy": "建议提前备货,锁定当前价格",
    "sell": "建议抓紧出货,兑现当前高价",
    "hold": "建议按需跟进,不追涨不囤货",
    "watch": "建议保持观望,等待更明确的信号",
}


def action_label(action: Optional[str]) -> str:
    return ACTION_LABELS_ZH.get((action or "watch").lower(), "建议观望")


def _fmt_price(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:,.0f}"


def _fmt_pct(v: Optional[float]) -> str:
    """Fraction (0.029) → signed percent (+2.9%)."""
    return "—" if v is None else f"{v * 100:+.1f}%"


def _fmt_prob(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def build_one_liner(
    *,
    action: str,
    p_rise: Optional[float],
    expected_change_pct: Optional[float],
    below_naive: bool = False,
    intel_event_count: int = 0,
    intel_bias: str = "neutral",
) -> str:
    # P0.4: Honesty-gate downgrade → honest "no edge" message, not "insufficient data"
    if below_naive:
        return "模型暂无统计优势,展示保守基线,建议观望"
    if p_rise is None and expected_change_pct is None:
        return "数据不足,建议观望"
    parts = []
    if expected_change_pct is not None:
        parts.append(f"预计{_fmt_pct(expected_change_pct)}")
    if p_rise is not None:
        parts.append(f"上涨概率 {_fmt_prob(p_rise)}")
    line = ",".join(parts) + f",{action_label(action)}"
    if intel_event_count > 0 and intel_bias in ("up", "down"):
        line += f"(情报{_INTEL_BIAS_ZH[intel_bias]} {intel_event_count} 条)"
    return line


def build_narrative(
    *,
    name_zh: str,
    day: int,
    current_price: Optional[float],
    forecast_base: Optional[float],
    expected_change_pct: Optional[float],
    p_rise: Optional[float],
    action: str,
    confidence: str,
    directional_accuracy: Optional[float],
    intel_event_count: int,
    intel_bias: str,
    volatility_regime: Optional[str],
    basis_zh: str = "",
    action_logic_zh: str = "",
    below_naive: bool = False,
    ensemble_mape_h7: Optional[float] = None,
    naive_mape_h7: Optional[float] = None,
) -> str:
    """Plain-language per-product narrative.

    When *basis_zh* / *action_logic_zh* are provided (from the forecast
    analysis block), the narrative explains WHY: trend / model-ensemble /
    uncertainty evidence, plus the threshold logic behind the action and
    what would change it. Otherwise it degrades to the legacy 3-sentence
    form.
    """
    # P0.4: distinct message for honesty-gate downgrade vs truly sparse data
    if below_naive:
        mape_info = ""
        if ensemble_mape_h7 is not None and naive_mape_h7 is not None:
            mape_info = f"(模型回测误差 {ensemble_mape_h7:.1f}% vs 基线 {naive_mape_h7:.1f}%)"
        return (
            f"{name_zh}当前模型暂无统计优势{mape_info},"
            f"展示保守基线预测,{_ACTION_ADVICE_ZH['watch']}。"
        )
    if forecast_base is None or current_price is None:
        return f"{name_zh}当前预测数据不足,{_ACTION_ADVICE_ZH['watch']}。"
    # Direction: prefer expected_change_pct; fall back to forecast vs current.
    if expected_change_pct is not None:
        move = "涨至" if expected_change_pct >= 0 else "跌至"
        pct_txt = f"({_fmt_pct(expected_change_pct)})"
    else:
        move = "涨至" if forecast_base >= current_price else "跌至"
        pct_txt = ""
    s1 = (
        f"{name_zh}现价 {_fmt_price(current_price)} 元/吨,"
        f"预计未来 {day} 天{move} {_fmt_price(forecast_base)} 元/吨{pct_txt}"
    )
    if p_rise is not None:
        s1 += f",上涨概率 {_fmt_prob(p_rise)}"
    s1 += "。"

    # 【预测依据】 trend / model / uncertainty (from analysis block)
    s_basis = f"预测依据:{basis_zh}" if basis_zh else ""

    evidence = []
    if directional_accuracy is not None:
        evidence.append(f"方向预测历史准确率 {_fmt_prob(directional_accuracy)}")
    if intel_event_count > 0:
        evidence.append(
            f"市场情报{_INTEL_BIAS_ZH.get(intel_bias, '中性')}"
            f"({intel_event_count} 条在册事件)"
        )
    if volatility_regime:
        evidence.append(
            f"波动状态{_VOLATILITY_ZH.get(volatility_regime, volatility_regime)}"
        )
    s2 = "、".join(evidence) + "。" if evidence else ""

    # 【建议逻辑】 threshold reasoning (from analysis block), else legacy advice
    if action_logic_zh:
        s3 = f"{_ACTION_ADVICE_ZH.get(action, _ACTION_ADVICE_ZH['watch'])} —— {action_logic_zh}"
    else:
        s3 = (
            f"{_ACTION_ADVICE_ZH.get(action, _ACTION_ADVICE_ZH['watch'])}"
            f"(置信度:{_CONFIDENCE_ZH.get(confidence, '低')})。"
        )
    return s1 + s_basis + s2 + s3


def build_briefing(products: list[DecisionBoardProduct], day: int) -> str:
    strong = [
        p for p in products
        if p.action in ("buy", "sell") and p.confidence != "low"
    ]
    if not strong:
        return (
            f"未来 {day} 天市场整体平稳,{len(products)} 个产品均无强买卖信号,"
            "建议按需跟进、控制库存风险。"
        )
    segs = []
    for p in strong:
        direction = "看涨" if p.action == "buy" else "看跌"
        prob = f"(上涨概率 {_fmt_prob(p.p_rise)})" if p.p_rise is not None else ""
        segs.append(f"{p.name_zh}{direction}{prob},{_ACTION_ADVICE_ZH[p.action]}")
    others = [p for p in products if p not in strong]
    tail = (
        "其余 " + "、".join(p.name_zh for p in others) + " 建议观望或按需跟进。"
        if others else ""
    )
    return f"未来 {day} 天重点关注:" + ";".join(segs) + "。" + tail


def _pick(items, day):
    """Return the overlay entry whose horizon == day, else None."""
    for it in items or []:
        if getattr(it, "horizon", None) == day:
            return it
    return None


def _build_decision_roi(product_id: str, db) -> Optional[DecisionRoiMeta]:
    """Query ForecastDecisionLog for a product and aggregate ROI stats.

    Returns None if no logs exist (graceful — most products have none yet
    since FORECAST_DECISION_LOGGING_ENABLED defaults OFF).
    """
    if db is None:
        return None
    try:
        from app.models.forecasting import ForecastDecisionLog
        from app.services.forecasting.features.decision_roi import aggregate_roi

        logs = (
            db.query(ForecastDecisionLog)
            .filter(ForecastDecisionLog.product_id == product_id)
            .all()
        )
        if not logs:
            return None
        summary = aggregate_roi(logs)
        return DecisionRoiMeta(
            total_decisions=summary.total_decisions,
            total_realized=summary.total_realized,
            buy_count=summary.buy_count,
            sell_count=summary.sell_count,
            accuracy_pct=summary.accuracy_pct,
            weighted_roi_pct=summary.weighted_roi,
            buy_correct=summary.buy_correct,
            sell_correct=summary.sell_correct,
        )
    except Exception:
        # Additive — never break the board on ROI computation failure.
        return None


def _surface(db):
    """Return a ``ForecastingAgentService`` when the unified flag is on, else None.

    The decision board composes market snapshots + forecast base batch +
    forecast analysis + analyst brief. When the unified service is enabled,
    all four reads route through one facade; otherwise the legacy direct
    ``mds`` / ``analyst_service`` calls remain active (flag off — default).
    """
    from app.config import settings

    if not settings.UNIFIED_FORECASTING_SERVICE_ENABLED:
        return None
    from app.services.forecasting.unified_service import ForecastingAgentService

    return ForecastingAgentService(db)


def build_decision_board(group: str, day: int = 7, db=None) -> DecisionBoardResponse:
    product_ids = PRODUCT_GROUP_MAP.get(group, [])
    svc = _surface(db)
    # get_market_snapshot enforces the EDIA fail-closed guard (503).
    snapshots = {
        s.id: s for s in (
            svc.get_market_snapshot(group)
            if svc is not None
            else mds.get_market_snapshot(group)
        )
    }
    batch = (
        svc.get_forecast_base_batch_v2(product_ids, day=day)
        if svc is not None
        else mds.get_forecast_base_batch_v2(product_ids, day=day, db=db)
    )

    products: list[DecisionBoardProduct] = []
    for pid in product_ids:
        snap = snapshots.get(pid)
        labels = PRODUCT_LABELS.get(pid, {})
        overlay = (batch.overlay_by_product or {}).get(pid)
        prob = _pick(overlay.probability if overlay else None, day)
        dec = _pick(overlay.decision if overlay else None, day)
        dire = _pick(overlay.directional if overlay else None, day)
        intel = overlay.intelligence if overlay else None
        policy = overlay.policy if overlay else None
        trust = overlay.trust_tier if overlay else None

        action = dec.action if dec else "watch"
        confidence = dec.confidence if dec else "low"
        p_rise = prob.p_rise if prob else None
        exp_chg = prob.expected_change_pct if prob else None
        intel_count = intel.active_event_count if intel else 0
        intel_bias = intel.bias_direction if intel else "neutral"
        vol = policy.volatility_regime if policy else None
        name_zh = labels.get("label_zh", pid)

        # P0.4: detect honesty-gate downgrade for distinct messaging
        _below_naive = (
            trust is not None
            and "below_naive_baseline" in (trust.reason_codes or [])
        )
        current = snap.price if snap else None
        base = (batch.base_by_product or {}).get(pid)

        trust_label = None
        if trust is not None:
            trust_label = trust.badge_label_zh or TRUST_LABELS_ZH.get(trust.tier)

        # AI 分析依据 (trend / model / uncertainty + action logic)
        analysis = None
        try:
            analysis = (
                svc.get_forecast_analysis(pid, day)
                if svc is not None
                else mds.get_forecast_analysis(pid, day, db=db)
            )
        except Exception:  # additive — never break the board on analysis failure
            analysis = None
        basis_zh = analysis.basis_zh if analysis else ""
        action_logic_zh = analysis.action_logic_zh if analysis else ""

        # Analyst brief (evidence-grounded LLM/template) — additive.
        # When present, its sections become the narrative; otherwise the
        # legacy template narrative is used.
        brief = None
        try:
            if svc is not None:
                brief = svc.get_forecast_brief(pid, day=day)
            else:
                from app.services.forecasting.analyst import service as analyst_service
                brief = analyst_service.get_analyst_brief(pid, day=day, db=db)
        except Exception:
            brief = None

        if brief:
            from app.services.forecasting.analyst.template_brief import SECTION_TITLES
            narrative_parts = []
            for key, title in SECTION_TITLES:
                val = brief.get(key, "")
                if val:
                    narrative_parts.append(f"{title}{val}")
            triggers = brief.get("watch_triggers_zh") or []
            if triggers:
                narrative_parts.append("【触发条件】" + ";".join(triggers))
            risk_val = brief.get("risk_zh", "")
            if risk_val:
                narrative_parts.append(f"【风险提示】{risk_val}")
            narrative_zh = "".join(narrative_parts)
            brief_meta = AnalystBriefMeta(
                **{k: (brief.get(k) or ("" if k != "watch_triggers_zh" else []))
                   for k in (
                    "market_update_zh", "price_data_zh", "upstream_logic_zh",
                    "supply_demand_zh", "forecast_zh", "watch_triggers_zh",
                    "risk_zh", "source", "day", "generated_at")}
            )
        else:
            # Extract per-horizon MAPEs for the narrative (from overlay, if available)
            _ensemble_mape_h7 = None
            _naive_mape_h7 = None
            if overlay and hasattr(overlay, "model_detail") and overlay.model_detail:
                md = overlay.model_detail if isinstance(overlay.model_detail, dict) else {}
                emh = md.get("ensemble_mape_by_horizon", {})
                nmh = md.get("naive_mape_by_horizon", {}) if "naive_mape_by_horizon" in md else {}
                if 7 in emh:
                    _ensemble_mape_h7 = emh[7]
                if 7 in nmh:
                    _naive_mape_h7 = nmh[7]
            narrative_zh = build_narrative(
                name_zh=name_zh, day=day, current_price=current,
                forecast_base=base, expected_change_pct=exp_chg, p_rise=p_rise,
                action=action, confidence=confidence,
                directional_accuracy=dire.accuracy if dire else None,
                intel_event_count=intel_count, intel_bias=intel_bias,
                volatility_regime=vol,
                basis_zh=basis_zh, action_logic_zh=action_logic_zh,
                below_naive=_below_naive,
                ensemble_mape_h7=_ensemble_mape_h7,
                naive_mape_h7=_naive_mape_h7,
            )
            brief_meta = None

        skus = [
            SkuBrief(material_code=s.material_code, name=s.name,
                     forecast_base=s.forecast_base)
            for s in (batch.sku_forecasts_by_product or {}).get(pid, [])
        ]

        # Stream B1: Decision ROI track record (additive — None if no logs).
        decision_roi = _build_decision_roi(pid, db)

        products.append(DecisionBoardProduct(
            id=pid,
            name_zh=name_zh,
            name_en=labels.get("label_en", pid),
            code=pid,
            current_price=current,
            price_date=snap.date if snap else None,
            forecast_base=base,
            expected_change_pct=exp_chg,
            p_rise=p_rise,
            action=action,
            action_label_zh=action_label(action),
            confidence=confidence,
            trust_tier=trust.tier if trust else None,
            trust_label_zh=trust_label,
            trust_color=trust.badge_color if trust else None,
            directional_accuracy=dire.accuracy if dire else None,
            intel_event_count=intel_count,
            intel_bias=intel_bias,
            volatility_regime=vol,
            one_liner_zh=build_one_liner(
                action=action, p_rise=p_rise, expected_change_pct=exp_chg,
                below_naive=_below_naive,
                intel_event_count=intel_count, intel_bias=intel_bias,
            ),
            narrative_zh=narrative_zh,
            analyst_brief=brief_meta,
            decision_roi=decision_roi,
            drivers=analysis.drivers if analysis else [],
            skus=skus,
        ))

    return DecisionBoardResponse(
        group=group,
        day=day,
        generated_at=datetime.now(timezone.utc).isoformat(),
        briefing_zh=build_briefing(products, day),
        products=products,
    )
