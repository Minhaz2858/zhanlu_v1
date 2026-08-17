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
    """Call LLM to generate market analysis text for a product."""
    from app.services.llm_service import call_llm

    price_trend = "上涨" if (change_pct_7d or 0) > 0 else "下跌" if (change_pct_7d or 0) < 0 else "持平"
    prompt = f"""你是化工市场分析师。请为以下产品撰写一段简明的市场分析（180-280字）。

**输出格式（必须遵守）：**
- 使用 Markdown 语法。
- 用 4 个加粗的小节标题（`**近期走势：**` `**驱动因素：**` `**供需面：**` `**短期展望：**`）作为开头。
- 每个小节用 1-2 句话描述，语言专业、简洁，避免空洞表述。
- 不要使用任何 emoji。

产品：{product_name_zh}
当前价格：{current_price or '暂无'} 元/吨
近7日涨跌幅：{change_pct_7d or 0}%
趋势方向：{price_trend}

近30日价格走势摘要：
{_summarize_prices(price_history_30d)}

预测数据：
{_summarize_forecast(forecast_data)}

请直接输出 Markdown 内容，不要包含任何解释或前言。"""

    try:
        result = await call_llm(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            task_type="weekly_digest",
        )
        return result.get("response", "") or ""
    except Exception as e:
        logger.error("LLM market_analysis failed for %s: %s", product_name_zh, e)
        return ""


async def _generate_ai_decision(
    product_name_zh: str,
    current_price: Optional[float],
    change_pct_7d: Optional[float],
    forecast_data: Optional[dict],
    market_analysis: str,
) -> str:
    """Call LLM to generate AI decision text for a product."""
    from app.services.llm_service import call_llm

    prompt = f"""你是化工行业决策顾问。基于以下信息，给出该产品的采购/销售策略建议（120-220字）。

**输出格式（必须遵守）：**
- 使用 Markdown 语法。
- 用 3 个加粗的小节标题（`**策略建议：**` `**依据：**` `**关键风险：**`）作为开头。
- **策略建议** 使用 Markdown 无序列表（`- 采购端：...` `- 销售端：...`）。
- 不要使用任何 emoji。

产品：{product_name_zh}
当前价格：{current_price or '暂无'} 元/吨
近7日涨跌幅：{change_pct_7d or 0}%
预测数据：
{_summarize_forecast(forecast_data)}

市场分析：
{market_analysis}

请直接输出 Markdown 内容，不要包含任何解释或前言。"""

    try:
        result = await call_llm(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            task_type="weekly_digest",
        )
        return result.get("response", "") or ""
    except Exception as e:
        logger.error("LLM ai_decision failed for %s: %s", product_name_zh, e)
        return ""


def _summarize_prices(prices: list[dict]) -> str:
    """Create a concise text summary of price history for the LLM prompt."""
    if not prices:
        return "暂无价格数据"
    vals = [p["price"] for p in prices if p.get("price") is not None]
    if not vals:
        return "暂无价格数据"
    return (
        f"最高 {max(vals):.0f}，最低 {min(vals):.0f}，"
        f"最新 {vals[-1]:.0f}，共 {len(vals)} 个数据点"
    )


def _summarize_forecast(forecast_data: Optional[dict]) -> str:
    """Create a concise text summary of forecast data for the LLM prompt."""
    if not forecast_data:
        return "暂无预测数据"
    lines = []
    for horizon, payload in forecast_data.items():
        base = payload.get("base", "N/A")
        bull = payload.get("bull", "N/A")
        bear = payload.get("bear", "N/A")
        conf = payload.get("confidence", 0.5)
        lines.append(f"{horizon}：基准 {base}，看涨 {bull}，看跌 {bear}，置信度 {conf:.0%}")
    return "\n".join(lines)


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
                    market_analysis=f"[生成失败] {str(e)[:200]}",
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
