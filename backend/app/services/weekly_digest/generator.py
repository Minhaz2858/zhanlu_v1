"""Weekly Summary & Forecast Digest Generator.

Core logic:
1. Read the product list from the forecast pack (include_in_weekly_report=true).
2. Fetch price history from MySQL warehouse via market_dashboard_service.
3. Read cached ForecastRun results from PostgreSQL.
4. Generate LLM market-analysis and AI-decision text per product.
5. Persist everything to weekly_report_cards + weekly_report_products.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models.weekly_digest import WeeklyReportCard, WeeklyReportProduct
from app.models.forecasting import ForecastTarget, ForecastRun
from app.models.market_dashboard import PRODUCT_LABELS

logger = logging.getLogger(__name__)

# ── Pack reader ──────────────────────────────────────────────────────────

_PACK_PATH = Path(__file__).resolve().parent.parent / "forecasting" / "packs" / "ecisco_c5c9_v1.json"


def _load_weekly_products() -> list[dict]:
    """Read the forecast pack and return products with include_in_weekly_report=true."""
    with open(_PACK_PATH, "r", encoding="utf-8") as f:
        pack = json.load(f)
    products = []
    for entry in pack.get("products", []):
        rc = entry.get("report_config", {})
        if rc.get("include_in_weekly_report"):
            products.append({
                "product_key": entry["product_key"],
                "report_order": rc.get("report_order", 99),
            })
    products.sort(key=lambda p: p["report_order"])
    return products


def _surface(db: Session):
    """Return a ``ForecastingAgentService`` when the unified flag is on, else None.

    The generator delegates its two external data reads (price history via
    ``market_dashboard_service`` and cached ForecastRun results via direct DB
    query) through the facade so callers consolidate on one API surface.
    ``None`` keeps the legacy inline paths active (flag off — the default).
    """
    from app.config import settings

    if not settings.UNIFIED_FORECASTING_SERVICE_ENABLED:
        return None
    from app.services.forecasting.unified_service import ForecastingAgentService

    return ForecastingAgentService(db)


# ── Price history helpers ────────────────────────────────────────────────

def _slice_history(
    rows: list[tuple], days: int, end_date: Optional[date] = None
) -> list[dict]:
    """Slice (date_str, price) tuples to the last N days, return [{date, price}]."""
    if not rows:
        return []
    cutoff = (end_date or date.today()) - timedelta(days=days)
    out = []
    for d_str, price in rows:
        if price is None:
            continue
        try:
            d = date.fromisoformat(str(d_str)[:10])
        except (ValueError, TypeError):
            continue
        if d >= cutoff:
            out.append({"date": str(d), "price": round(float(price), 2)})
    return out


def _current_price_and_change(
    rows: list[tuple], lookback_days: int = 7
) -> tuple[Optional[float], Optional[float]]:
    """Return (latest_price, change_pct_over_lookback_days) from history rows."""
    if not rows:
        return None, None
    # rows are sorted ascending by date
    latest_price = None
    for d_str, price in reversed(rows):
        if price is not None:
            latest_price = float(price)
            break
    if latest_price is None:
        return None, None
    # Find the price ~lookback_days ago
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    past_price = None
    for d_str, price in rows:
        if str(d_str)[:10] <= cutoff and price is not None:
            past_price = float(price)
    if past_price and past_price > 0:
        change_pct = round((latest_price - past_price) / past_price * 100, 2)
    else:
        change_pct = None
    return round(latest_price, 2), change_pct


# ── Forecast helpers ────────────────────────────────────────────────────

_CONFIDENCE_MAP = {"high": 0.85, "medium": 0.65, "low": 0.45}


def _terminal_price(value):
    """ForecastRun stores base/bull/bear as per-day arrays; return the terminal price."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[-1]
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _confidence_value(run) -> Optional[float]:
    """Map ForecastRun.confidence (High/Medium/Low) to a 0-1 float."""
    raw = getattr(run, "confidence", None)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    return _CONFIDENCE_MAP.get(str(raw).strip().lower())


def _read_forecast_for_product(
    product_key: str, db: Session
) -> Optional[dict]:
    """Read the latest ForecastRun results for a product, return {7d, 30d} payloads."""
    # ForecastTarget.product_key is namespaced with an "ecisco." prefix for the
    # active (forecast-populated) targets, while bare keys are "discovered"
    # placeholders without ForecastRun rows. Prefer the namespaced target.
    target_keys = [product_key]
    if not product_key.startswith("ecisco."):
        target_keys = [f"ecisco.{product_key}", product_key]
    tgt = None
    for key in target_keys:
        candidate = db.query(ForecastTarget).filter(
            ForecastTarget.product_key == key,
            ForecastTarget.org_id == "default-org",
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).first()
        if candidate is not None and db.query(ForecastRun.id).filter(
            ForecastRun.target_id == candidate.id,
            ForecastRun.is_deleted == False,  # noqa: E712
        ).first() is not None:
            tgt = candidate
            break
    if tgt is None:
        return None
    run = db.query(ForecastRun).filter(
        ForecastRun.target_id == tgt.id,
        ForecastRun.is_deleted == False,  # noqa: E712
    ).order_by(ForecastRun.created_date.desc()).first()
    if run is None or not run.results:
        return None
    results = run.results
    confidence = _confidence_value(run)
    out = {}
    for horizon in ("7d", "30d"):
        key = horizon  # "7d" or "30d"
        alt_key = horizon.rstrip("d")  # "7" or "30"
        payload = results.get(key) or results.get(alt_key)
        if not payload:
            continue
        base = _terminal_price(payload.get("base"))
        bull = _terminal_price(payload.get("bull"))
        bear = _terminal_price(payload.get("bear"))
        if base is None and bull is None and bear is None:
            continue
        out[horizon] = {
            "base": base,
            "bull": bull,
            "bear": bear,
            "confidence": confidence,
        }
    return out if out else None


# ── LLM generation ──────────────────────────────────────────────────────

async def _generate_market_analysis(
    product_name_zh: str,
    price_history_30d: list[dict],
    current_price: Optional[float],
    change_pct_7d: Optional[float],
    forecast_data: Optional[dict],
) -> str:
    """Call LLM to generate market analysis text for a product.

    Always returns non-empty content: falls back to a deterministic template
    built from the same real numbers when the LLM returns empty or raises.
    """
    from app.services.llm_service import call_llm

    heading = f"【{product_name_zh}市场分析】"
    prompt = f"""你是化工市场分析师。请为以下产品撰写市场分析。

输出格式（必须遵守）：
- 第一行是标题：`{heading}`
- 接着输出 3 个自然段（纯文本，禁止使用 Markdown 加粗、标题、列表符号或 emoji）：
  1. 近期价格走势：必须引用下方"数据"中的真实数字（30日起始价、最新价、30日涨跌幅、近7日涨跌幅）。
  2. 供需面分析。
  3. 短期展望：引用下方预测数据中的 7日/30日基准价与区间。
- 反杜撰要求：文中出现的所有数字必须从下方"数据"中复制，禁止编造任何未提供的数字。

数据：
产品：{product_name_zh}
当前价格：{current_price if current_price is not None else '暂无'} 元/吨
近7日涨跌幅：{change_pct_7d if change_pct_7d is not None else '暂无'}%

近30日价格走势摘要：
{_summarize_prices(price_history_30d)}

预测数据：
{_summarize_forecast(forecast_data, current_price)}

请直接输出正文，不要包含任何解释或前言。"""

    text = ""
    try:
        result = await call_llm(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            task_type="weekly_digest",
        )
        text = _sanitize_llm_text(result.get("response", "") or "")
    except Exception as e:
        logger.error("LLM market_analysis failed for %s: %s", product_name_zh, e)

    if not text.startswith(heading):
        text = f"{heading}\n{text.strip()}".strip() if text.strip() else ""

    if not text or text == heading:
        logger.warning("market_analysis fallback engaged for %s", product_name_zh)
        return _fallback_market_analysis(
            product_name_zh, current_price, change_pct_7d, price_history_30d, forecast_data
        )
    return text


async def _generate_ai_decision(
    product_name_zh: str,
    current_price: Optional[float],
    change_pct_7d: Optional[float],
    forecast_data: Optional[dict],
    market_analysis: str,
) -> str:
    """Call LLM to generate AI decision text for a product.

    Always returns non-empty content: falls back to a deterministic template
    built from the same real numbers when the LLM returns empty or raises.
    """
    from app.services.llm_service import call_llm

    prompt = f"""你是化工行业决策顾问。基于以下信息，给出该产品的采购/销售策略建议。

输出格式（必须遵守）：
- 输出 3 行纯文本（禁止使用 Markdown 加粗、标题、列表符号或 emoji）：
  1. 策略建议：...
  2. 依据：...（必须引用下方数据中的真实数字）
  3. 关键风险：...
- 反杜撰要求：文中出现的所有数字必须从下方"数据"中复制，禁止编造任何未提供的数字。

数据：
产品：{product_name_zh}
当前价格：{current_price if current_price is not None else '暂无'} 元/吨
近7日涨跌幅：{change_pct_7d if change_pct_7d is not None else '暂无'}%
预测数据：
{_summarize_forecast(forecast_data, current_price)}

市场分析：
{market_analysis}

请直接输出正文，不要包含任何解释或前言。"""

    text = ""
    try:
        result = await call_llm(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            task_type="weekly_digest",
        )
        text = _sanitize_llm_text(result.get("response", "") or "")
    except Exception as e:
        logger.error("LLM ai_decision failed for %s: %s", product_name_zh, e)

    if not text.strip():
        logger.warning("ai_decision fallback engaged for %s", product_name_zh)
        return _fallback_ai_decision(
            product_name_zh, current_price, change_pct_7d, forecast_data
        )
    return text


def _summarize_prices(prices: list[dict]) -> str:
    """Create a concise text summary of price history for the LLM prompt."""
    if not prices:
        return "暂无价格数据"
    vals = [p["price"] for p in prices if p.get("price") is not None]
    if not vals:
        return "暂无价格数据"
    start = vals[0]
    end = vals[-1]
    pct_30d = (end - start) / start * 100 if start else 0.0
    window_7d = vals[-7:]
    start_7d = window_7d[0]
    pct_7d = (end - start_7d) / start_7d * 100 if start_7d else 0.0
    return (
        f"起始 {start:.0f} 元/吨 → 最新 {end:.0f} 元/吨，"
        f"30日涨跌幅 {pct_30d:+.2f}%，近7日涨跌幅 {pct_7d:+.2f}%；"
        f"区间最高 {max(vals):.0f}，最低 {min(vals):.0f}，共 {len(vals)} 个数据点"
    )


def _summarize_forecast(forecast_data: Optional[dict], current_price: Optional[float]) -> str:
    """Create a concise text summary of forecast data for the LLM prompt.

    Includes the base/bull/bear numbers plus each scenario's delta vs the
    current price so the LLM (and fallback) can cite real direction.
    """
    if not forecast_data:
        return "暂无预测数据"
    lines = []
    for horizon, payload in forecast_data.items():
        base = payload.get("base")
        bull = payload.get("bull")
        bear = payload.get("bear")
        conf = payload.get("confidence") or 0.5

        def _f(v):
            return "N/A" if v is None else f"{v:.0f}"

        base_txt = _f(base)
        if base is not None and current_price:
            delta = (base - current_price) / current_price * 100
            base_txt += f"（较现价 {delta:+.1f}%）"
        lines.append(
            f"{horizon}：基准 {base_txt}，看涨 {_f(bull)}，"
            f"看跌 {_f(bear)}，置信度 {conf:.0%}"
        )
    return "\n".join(lines)


def _sanitize_llm_text(text: str) -> str:
    """Strip markdown bold/heading/list markers so output matches the sample.

    Removes ``**``, leading ``#`` heading markers, and leading ``- `` / ``1. `` /
    ``1. ``-style list markers line-by-line.
    """
    if not text:
        return ""
    text = text.replace("**", "")
    out = []
    for line in text.splitlines():
        s = line.strip()
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^[-*+]\s+", "", s)
        s = re.sub(r"^\d+[.、)]\s*", "", s)
        out.append(s)
    return "\n".join(out).strip()


def _fallback_market_analysis(
    product_name_zh: str,
    current_price: Optional[float],
    change_pct_7d: Optional[float],
    price_history_30d: list[dict],
    forecast_data: Optional[dict],
) -> str:
    """Deterministic market-analysis template built only from real numbers."""
    heading = f"【{product_name_zh}市场分析】"

    if price_history_30d and current_price is not None:
        start = price_history_30d[0].get("price")
        if start:
            pct_30d = (current_price - start) / start * 100
            trend = "上涨" if pct_30d > 0 else "下跌" if pct_30d < 0 else "持平"
            p1 = (
                f"近30日价格由 {start:.0f} 元/吨 波动至 {current_price:.0f} 元/吨，"
                f"区间涨跌幅 {pct_30d:+.2f}%，整体呈{trend}态势。"
            )
        else:
            p1 = f"当前价格 {current_price:.0f} 元/吨。"
        if change_pct_7d is not None:
            t7 = "上涨" if change_pct_7d > 0 else "下跌" if change_pct_7d < 0 else "持平"
            p1 += f"近7日{t7} {abs(change_pct_7d):.2f}%。"
    else:
        p1 = "暂无价格数据，无法判断近期价格走势。"

    p2 = "供需面暂无结构化数据，建议结合库存与开工率持续跟踪供需变化。"

    if forecast_data:
        parts = []
        for horizon, label in (("7d", "7日"), ("30d", "30日")):
            payload = forecast_data.get(horizon) or {}
            base = payload.get("base")
            bull = payload.get("bull")
            bear = payload.get("bear")
            if base is None or bull is None or bear is None:
                continue
            parts.append(f"{label}预测基准 {base:.0f} 元/吨（区间 {bear:.0f}~{bull:.0f}）")
        p3 = "短期展望：" + "，".join(parts) + "。" if parts else "暂无有效预测数据，短期方向待观察。"
    else:
        p3 = "暂无预测数据，短期方向待观察。"

    return "\n".join([heading, p1, p2, p3])


def _fallback_ai_decision(
    product_name_zh: str,
    current_price: Optional[float],
    change_pct_7d: Optional[float],
    forecast_data: Optional[dict],
) -> str:
    """Deterministic AI-decision template built only from real numbers."""
    base7 = (forecast_data or {}).get("7d", {}).get("base")
    if base7 is not None and current_price:
        delta = (base7 - current_price) / current_price * 100
        rec = "看涨" if delta > 0.5 else "看跌" if delta < -0.5 else "震荡"
    elif change_pct_7d is not None:
        rec = "偏强" if change_pct_7d > 0 else "偏弱" if change_pct_7d < 0 else "观望"
    else:
        rec = "观望"

    basis = f"当前价格 {current_price:.0f} 元/吨" if current_price is not None else "暂无价格数据"
    if base7 is not None:
        basis += f"，7日预测基准 {base7:.0f} 元/吨"
    if change_pct_7d is not None:
        basis += f"，近7日涨跌幅 {change_pct_7d:+.2f}%"

    risk = "价格波动、供需变化及预测偏差为主要风险，需结合实时数据动态调整。"

    return "\n".join([
        f"策略建议：{rec}，建议结合库存与需求节奏灵活操作。",
        f"依据：{basis}。",
        f"关键风险：{risk}",
    ])


# ── Report-level LLM sections ───────────────────────────────────────────


def _aggregate_for_summary(product_rows: list) -> dict:
    """Compute deterministic headline stats across all product rows."""
    priced = [p for p in product_rows if getattr(p, "current_price", None) is not None]
    up = sum(1 for p in priced if (getattr(p, "change_pct_7d", None) or 0) > 0)
    down = sum(1 for p in priced if (getattr(p, "change_pct_7d", None) or 0) < 0)
    flat = len(priced) - up - down

    movers = sorted(priced, key=lambda p: getattr(p, "change_pct_7d", None) or 0, reverse=True)
    gainers = movers[:3]
    losers = sorted(priced, key=lambda p: getattr(p, "change_pct_7d", None) or 0)[:3]

    bull = bear = 0
    for p in product_rows:
        f7 = getattr(p, "forecast_7d", None) or {}
        base = f7.get("base")
        bull_v = f7.get("bull")
        if base is None or bull_v is None:
            continue
        if bull_v > base:
            bull += 1
        elif bull_v < base:
            bear += 1

    return {
        "total": len(product_rows),
        "priced": len(priced),
        "up": up,
        "down": down,
        "flat": flat,
        "gainers": gainers,
        "losers": losers,
        "bull": bull,
        "bear": bear,
    }


def _product_summary_lines(product_rows: list) -> list:
    """Build one human-readable line per product for the LLM summary prompt."""
    lines = []
    for p in product_rows:
        name = getattr(p, "product_name_zh", "") or getattr(p, "product_key", "?")
        price = getattr(p, "current_price", None)
        chg = getattr(p, "change_pct_7d", None)
        price_txt = f"{price:,.0f} 元/吨" if price is not None else "暂无价格"
        chg_txt = f"{chg:+.1f}%" if chg is not None else "N/A"
        f7 = getattr(p, "forecast_7d", None) or {}
        base = f7.get("base")
        bull_v = f7.get("bull")
        bear_v = f7.get("bear")
        if base is not None and bull_v is not None and bear_v is not None:
            rng_txt = f"{min(bear_v, bull_v):,.0f} ~ {max(bear_v, bull_v):,.0f}"
        else:
            rng_txt = "暂无预测"
        lines.append(f"- {name}：现价 {price_txt}，周涨跌 {chg_txt}，预测区间 {rng_txt}")
    return lines


async def _generate_market_overview(agg: dict, product_lines: list) -> str:
    """LLM-generated 市场概况 / 原料情况 narrative (report opener)."""
    from app.services.llm_service import call_llm

    def _fmt(items):
        return "、".join(
            f"{getattr(p, 'product_name_zh', '')}({getattr(p, 'change_pct_7d', 0) or 0:+.1f}%)"
            for p in items
        ) or "无"

    prompt = f"""你是化工行业首席分析师。基于以下 C5C9 系列产品的周度数据，撰写报告开篇的"市场概况/原料情况"章节（300-450字）。

**输出格式（必须遵守）：**
- 使用 Markdown 语法。
- 用 3 个加粗小节标题作为开头：`**市场概况：**` `**原料与成本：**` `**整体走势：**`。
- 先概述本周宏观与原料（原油/石脑油/裂解原料）背景，再总结整体涨跌格局，最后点明主要驱动因素。
- 语言专业、简洁，不要 emoji，不要逐产品罗列明细（明细见下文各产品卡片）。

整体统计：
- 已定价产品 {agg['priced']} 个，上涨 {agg['up']} 个，下跌 {agg['down']} 个，持平 {agg['flat']} 个
- 领涨品种：{_fmt(agg['gainers'])}
- 领跌品种：{_fmt(agg['losers'])}
- 预测方向：看涨 {agg['bull']} 个，看跌 {agg['bear']} 个

各产品概览：
{chr(10).join(product_lines)}

请直接输出 Markdown 内容，不要包含任何解释或前言。"""

    try:
        result = await call_llm(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            task_type="weekly_digest",
        )
        return result.get("response", "") or ""
    except Exception as e:
        logger.error("LLM market_overview failed: %s", e)
        return ""


async def _generate_executive_summary(agg: dict, product_lines: list) -> str:
    """LLM-generated 总结预测及建议 (numbered recommendations, report closer)."""
    from app.services.llm_service import call_llm

    prompt = f"""你是化工行业决策顾问。基于以下周度数据，撰写报告结尾的"总结预测及建议"章节。

**输出格式（必须遵守）：**
- 使用 Markdown 语法。
- 用 2 个加粗小节标题作为开头：`**总结预测：**` `**操作建议：**`。
- `**总结预测：**` 用 2-3 句话概括本周市场整体走向与下周展望。
- `**操作建议：**` 使用 Markdown 有序列表（`1.` `2.` `3.` `4.`），共 3-4 条，每条 30-50 字，具体可执行（采购节奏、库存策略、价格区间关注、风险对冲）。
- 不要使用任何 emoji。

整体统计：
- 已定价产品 {agg['priced']} 个，上涨 {agg['up']} 个，下跌 {agg['down']} 个，持平 {agg['flat']} 个
- 预测方向：看涨 {agg['bull']} 个，看跌 {agg['bear']} 个

各产品概览：
{chr(10).join(product_lines)}

请直接输出 Markdown 内容，不要包含任何解释或前言。"""

    try:
        result = await call_llm(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            task_type="weekly_digest",
        )
        return result.get("response", "") or ""
    except Exception as e:
        logger.error("LLM executive_summary failed: %s", e)
        return ""


async def _finalize_summaries(card: WeeklyReportCard, product_rows: list) -> None:
    """Aggregate product rows and generate the two report-level LLM sections."""
    agg = _aggregate_for_summary(product_rows)
    lines = _product_summary_lines(product_rows)
    overview, exec_summary = await asyncio.gather(
        _generate_market_overview(agg, lines),
        _generate_executive_summary(agg, lines),
    )
    card.market_overview = overview or None
    card.executive_summary = exec_summary or None


# ── Main generator ──────────────────────────────────────────────────────

async def generate_weekly_digest(db: Session, org_id: str = "default-org") -> WeeklyReportCard:
    """Generate a weekly digest report for all configured products.

    Steps:
    1. Create a WeeklyReportCard row (status=generating).
    2. For each product: fetch price history, read forecasts, generate LLM text.
    3. Persist WeeklyReportProduct rows.
    4. Update card status to "ready" (or "failed").
    """
    from app.services.market_dashboard_service import read_product_history_rows

    svc = _surface(db)

    # ── Determine week range ──
    today = date.today()
    # Last full week: Monday to Sunday
    week_end = today - timedelta(days=today.weekday() + 1) if today.weekday() < 6 else today
    if week_end >= today:
        week_end = today - timedelta(days=today.weekday() + 1)
    week_start = week_end - timedelta(days=6)

    # ── Create card ──
    card = WeeklyReportCard(
        org_id=org_id,
        app_id="ecisco-bi",
        week_start=week_start,
        week_end=week_end,
        title_zh=f"C5C9周度总结及预测（{week_start.month}.{week_start.day}-{week_end.month}.{week_end.day}）",
        title_en=f"C5/C9 Weekly Summary & Forecast ({week_start.strftime('%b %d')}-{week_end.strftime('%b %d')})",
        product_count=0,
        status="generating",
    )
    db.add(card)
    db.commit()
    db.refresh(card)

    products = _load_weekly_products()
    card.product_count = len(products)
    db.commit()

    # ── Generate per-product data ──
    success_count = 0
    errors = []

    for prod_info in products:
        pkey = prod_info["product_key"]
        order = prod_info["report_order"]
        labels = PRODUCT_LABELS.get(pkey, {})
        name_zh = labels.get("label_zh", pkey)
        name_en = labels.get("label_en", pkey.replace("_", " ").title())

        try:
            # Price history
            history_rows = (
                svc.read_product_history_rows(pkey)
                if svc is not None
                else read_product_history_rows(pkey)
            )
            price_12mo = _slice_history(history_rows, 365)
            price_30d = _slice_history(history_rows, 30)
            current_price, change_pct_7d = _current_price_and_change(history_rows, 7)

            # Forecast
            forecast_data = (
                svc.get_weekly_forecast_payloads(pkey)
                if svc is not None
                else _read_forecast_for_product(pkey, db)
            )
            forecast_7d = forecast_data.get("7d") if forecast_data else None
            forecast_30d = forecast_data.get("30d") if forecast_data else None

            # LLM generation (sequential within product, parallel across products is done at scheduler level)
            market_analysis = await _generate_market_analysis(
                name_zh, price_30d, current_price, change_pct_7d, forecast_data
            )
            ai_decision = await _generate_ai_decision(
                name_zh, current_price, change_pct_7d, forecast_data, market_analysis
            )

            product_row = WeeklyReportProduct(
                org_id=org_id,
                app_id="ecisco-bi",
                report_id=card.id,
                product_key=pkey,
                product_name_zh=name_zh,
                product_name_en=name_en,
                report_order=order,
                current_price=current_price,
                change_pct_7d=change_pct_7d,
                price_12mo=price_12mo,
                price_30d=price_30d,
                market_analysis=market_analysis,
                forecast_7d=forecast_7d,
                forecast_30d=forecast_30d,
                ai_decision=ai_decision,
            )
            db.add(product_row)
            success_count += 1

        except Exception as e:
            logger.error("Weekly digest product %s failed: %s", pkey, e)
            errors.append(f"{name_zh}: {e}")
            # Still add a partial row so the UI shows the product
            product_row = WeeklyReportProduct(
                org_id=org_id,
                app_id="ecisco-bi",
                report_id=card.id,
                product_key=pkey,
                product_name_zh=name_zh,
                product_name_en=name_en,
                report_order=order,
                market_analysis=_fallback_market_analysis(name_zh, None, None, [], None),
                ai_decision=_fallback_ai_decision(name_zh, None, None, None),
            )
            db.add(product_row)

    # ── Finalize card ──
    card.status = "ready" if success_count > 0 else "failed"
    card.error_msg = "; ".join(errors) if errors else None
    card.generated_at = datetime.utcnow()
    db.commit()
    db.refresh(card)

    logger.info(
        "Weekly digest generated: card=%s, success=%d/%d, status=%s",
        card.id, success_count, len(products), card.status,
    )
    return card


async def generate_weekly_digest_parallel(db: Session, org_id: str = "default-org") -> WeeklyReportCard:
    """Generate weekly digest with parallel LLM calls per product.

    Uses asyncio.gather with a semaphore to limit concurrency to 3.
    """
    from app.services.market_dashboard_service import read_product_history_rows

    svc = _surface(db)

    sem = asyncio.Semaphore(3)

    today = date.today()
    week_end = today - timedelta(days=today.weekday() + 1) if today.weekday() < 6 else today
    if week_end >= today:
        week_end = today - timedelta(days=today.weekday() + 1)
    week_start = week_end - timedelta(days=6)

    title_zh = f"C5C9周度总结及预测（{week_start.month}.{week_start.day}-{week_end.month}.{week_end.day}）"
    title_en = f"C5/C9 Weekly Summary & Forecast ({week_start.strftime('%b %d')}-{week_end.strftime('%b %d')})"

    # Reuse an existing card for this week (regenerate) so a manual re-trigger
    # does not violate the unique (org_id, week_start) constraint.
    card = db.query(WeeklyReportCard).filter(
        WeeklyReportCard.org_id == org_id,
        WeeklyReportCard.week_start == week_start,
        WeeklyReportCard.is_deleted == False,  # noqa: E712
    ).first()
    if card is not None:
        db.query(WeeklyReportProduct).filter(
            WeeklyReportProduct.report_id == card.id
        ).delete(synchronize_session=False)
        card.title_zh = title_zh
        card.title_en = title_en
        card.status = "generating"
        card.error_msg = None
        card.market_overview = None
        card.executive_summary = None
        card.generated_at = None
        card.product_count = 0
    else:
        card = WeeklyReportCard(
            org_id=org_id,
            app_id="ecisco-bi",
            week_start=week_start,
            week_end=week_end,
            title_zh=title_zh,
            title_en=title_en,
            product_count=0,
            status="generating",
        )
        db.add(card)
    db.commit()
    db.refresh(card)

    products = _load_weekly_products()
    card.product_count = len(products)
    db.commit()

    async def _process_product(prod_info: dict) -> WeeklyReportProduct:
        async with sem:
            pkey = prod_info["product_key"]
            order = prod_info["report_order"]
            labels = PRODUCT_LABELS.get(pkey, {})
            name_zh = labels.get("label_zh", pkey)
            name_en = labels.get("label_en", pkey.replace("_", " ").title())

            try:
                history_rows = (
                    svc.read_product_history_rows(pkey)
                    if svc is not None
                    else read_product_history_rows(pkey)
                )
                price_12mo = _slice_history(history_rows, 365)
                price_30d = _slice_history(history_rows, 30)
                current_price, change_pct_7d = _current_price_and_change(history_rows, 7)

                forecast_data = (
                    svc.get_weekly_forecast_payloads(pkey)
                    if svc is not None
                    else _read_forecast_for_product(pkey, db)
                )
                forecast_7d = forecast_data.get("7d") if forecast_data else None
                forecast_30d = forecast_data.get("30d") if forecast_data else None

                market_analysis = await _generate_market_analysis(
                    name_zh, price_30d, current_price, change_pct_7d, forecast_data
                )
                ai_decision = await _generate_ai_decision(
                    name_zh, current_price, change_pct_7d, forecast_data, market_analysis
                )

                return WeeklyReportProduct(
                    org_id=org_id,
                    app_id="ecisco-bi",
                    report_id=card.id,
                    product_key=pkey,
                    product_name_zh=name_zh,
                    product_name_en=name_en,
                    report_order=order,
                    current_price=current_price,
                    change_pct_7d=change_pct_7d,
                    price_12mo=price_12mo,
                    price_30d=price_30d,
                    market_analysis=market_analysis,
                    forecast_7d=forecast_7d,
                    forecast_30d=forecast_30d,
                    ai_decision=ai_decision,
                ), False
            except Exception as e:
                logger.error("Weekly digest product %s failed: %s", pkey, e)
                return WeeklyReportProduct(
                    org_id=org_id,
                    app_id="ecisco-bi",
                    report_id=card.id,
                    product_key=pkey,
                    product_name_zh=name_zh,
                    product_name_en=name_en,
                    report_order=order,
                    market_analysis=_fallback_market_analysis(name_zh, None, None, [], None),
                    ai_decision=_fallback_ai_decision(name_zh, None, None, None),
                ), True

    results = await asyncio.gather(*[_process_product(p) for p in products], return_exceptions=True)

    success_count = 0
    errors = []
    product_rows = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            errors.append(f"product {products[i]['product_key']}: {result}")
            continue
        if isinstance(result, tuple):
            product_row, is_error = result
            db.add(product_row)
            product_rows.append(product_row)
            if is_error:
                errors.append(f"{product_row.product_name_zh}: 生成失败")
            else:
                success_count += 1
        elif isinstance(result, WeeklyReportProduct):
            db.add(result)
            product_rows.append(result)
            success_count += 1

    # Report-level sections (market overview + executive summary).
    if product_rows:
        await _finalize_summaries(card, product_rows)

    card.status = "ready" if success_count > 0 else "failed"
    card.error_msg = "; ".join(errors) if errors else None
    card.generated_at = datetime.utcnow()
    db.commit()
    db.refresh(card)

    logger.info(
        "Weekly digest generated (parallel): card=%s, success=%d/%d, status=%s",
        card.id, success_count, len(products), card.status,
    )
    return card
