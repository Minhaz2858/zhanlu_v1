"""Ecisco BI Weekly Report module — Phase 5E.

Port of EDIA 5.1.2 backend/src/service/weekly_report_generator.py (§4).
Adapted: EDIA SQLite → Zhanlu async SQLAlchemy, EDIA threading → asyncio,
EDIA file persistence → DB persistence via zhanlu-postgres.

Pipeline:
  1. RESOLVE_WEEK → compute report week from current date
  2. LOAD_PRODUCT_CATALOG → all 12 C5/C9 products
  3. FETCH_MACRO_CONTEXT → crude/naphtha/forecast
  4. PER_PRODUCT_LOOP → summary + chart stub
  5. BUILD_SUMMARY_RECOMMENDATIONS
  6. ASSEMBLE_REPORT_MD
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from .data import resolve_week_dates, collect_all_numbers, build_prerendered_ctx
from .charts import render_chart_stub
from .parser import parse_report_to_sections


# ── System prompt (§8) ─────────────────────────────────────────

_SYSTEM_PROMPT = """你是惠州伊斯科（Ecisco）的石化市场分析助手。你的任务是根据提供的结构化数据，撰写产品的"市场分析"段落，用于公司周报。

严格规则：
1. 只能使用 INPUT_JSON 中已提供的数字和事实。禁止自行添加任何数字、价格、百分比、日期或公司名称。
2. 禁止在"市场分析"段落中给出价格预测或预测区间。预测内容由其他模块生成。
3. 输出必须是简体中文，不混入英文或拼音。
4. 输出必须是 3 个自然段，结构如下：
   - 第一段：回顾本周上游/原料/原油/石脑油行情，说明成本端变化。
   - 第二段：产品的价格走势、供需关系、主要影响因素分析。
   - 第三段：下周前瞻（趋势判断，不包含具体预测数字）。

示例格式：
[产品名称]
本周商品市场...
本周XXXX价格运行...
下周XXXX市场预计...
"""


class WeeklyReportService:
    """Orchestrate the weekly report pipeline for Ecisco BI."""

    def __init__(self, db=None, org_id: str | None = None, llm_caller=None):
        self.db = db
        self.org_id = org_id
        self.llm = llm_caller

    def generate_report(
        self,
        report_week: str | None = None,
        products: list[str] | None = None,
        include_charts: bool = True,
        include_forecast: bool = True,
    ) -> dict:
        """Generate a full weekly report for the specified week.

        Args:
            report_week: ISO week "2026-W31" or "2026-08-03". Default: current week.
            products: Product IDs to include. Default: all 12.
            include_charts: Generate chart stubs.
            include_forecast: Include forecast band data.

        Returns:
            {report_id, report_week, markdown, sections, products, generated_at}
        """
        # Step 1: Resolve week dates
        week_start, week_end, week_label = resolve_week_dates(report_week)

        # Step 2: Load product catalog
        if products is None:
            from app.services.knowledge_graph.graph import build_c5_c9_graph
            graph = build_c5_c9_graph()
            products = [n.id for n in graph.nodes]
        # Filter to known products only
        known = [p for p in products if p in {
            "isoprene", "piperylene", "dcpd", "cracked_c5", "mixed_c5",
            "cracked_c9", "raffinate_c5", "sis", "c5_resin",
            "blowing_agent", "styrene", "crude_oil", "naphtha",
        }]
        if not known:
            known = ["isoprene", "piperylene", "dcpd", "cracked_c5", "sis"]

        # Step 3: Fetch macro context
        macro_context = self._fetch_macro_context(week_start, week_end)

        # Step 4: Per-product loop
        product_sections: list[dict] = []
        for pid in known:
            if pid in ("crude_oil", "naphtha"):
                continue
            section = self._build_product_section(
                pid, week_start, week_end, macro_context,
                include_charts, include_forecast,
            )
            product_sections.append(section)

        # Step 5: Summary recommendations
        summary = self._build_summary_recommendations(product_sections, macro_context)

        # Step 6: Assemble markdown
        markdown = self._assemble_markdown(
            week_label, week_start, week_end, macro_context, product_sections, summary,
        )

        report_id = str(uuid.uuid4())[:8]
        generated_at = datetime.now(timezone.utc).isoformat()

        return {
            "report_id": report_id,
            "report_week": week_label,
            "week_start": week_start,
            "week_end": week_end,
            "markdown": markdown,
            "sections": product_sections,
            "summary": summary,
            "products": known,
            "generated_at": generated_at,
        }

    def _fetch_macro_context(self, week_start: str, week_end: str) -> dict:
        """Fetch crude/naphtha prices and forecasts for the report week."""
        context = {
            "week_start": week_start, "week_end": week_end,
            "crude_price": None, "crude_change_pct": 0.0,
            "naphtha_price": None, "naphtha_change_pct": 0.0,
            "naphtha_forecast_next_7d": None,
        }
        if self.db:
            try:
                from app.services.market_dashboard.service import MarketDashboardService
                svc = MarketDashboardService(self.db)
                snap = svc.build_snapshot()
                for pid in ("crude_oil", "naphtha"):
                    if pid in snap:
                        context[f"{pid.replace('crude_oil', 'crude')}_price"] = snap[pid].get("latest_price")
                        context[f"{pid.replace('crude_oil', 'crude')}_change_pct"] = snap[pid].get("change_7d_pct", 0)
            except Exception as exc:
                logger.debug("Could not fetch macro context: %s", exc)
        return context

    def _build_product_section(
        self, pid: str, week_start: str, week_end: str,
        macro: dict, include_charts: bool, include_forecast: bool,
    ) -> dict:
        """Build a single product section with narrative and chart stub."""
        from app.services.knowledge_graph.graph import build_c5_c9_graph
        graph = build_c5_c9_graph()
        node = graph.get_node(pid)
        name_cn = node.name_cn if node else pid

        # Collect numbers from pricing + perception services
        numbers = collect_all_numbers(pid, self.db)
        chart = render_chart_stub(pid, numbers, week_start, week_end) if include_charts else None

        # Build narrative: analyst brief (evidence-grounded) → placeholder fallback
        narrative = None
        try:
            from app.services.forecasting.analyst import service as analyst_service
            brief = analyst_service.get_analyst_brief(pid, day=7, db=self.db)
            if brief:
                narrative = (
                    brief.get("summary_zh", "")
                    + brief.get("why_price_zh", "")
                    + brief.get("why_action_zh", "")
                ) or None
        except Exception as exc:
            logger.warning("[weekly-report] analyst brief failed for %s: %s", pid, exc)
        if not narrative:
            narrative = (
                f"本周{name_cn}市场运行平稳。"
                f"上游原料端变化幅度有限，需求端按需采购。"
                f"下周市场预计延续当前态势，需关注宏观面变化。"
            )

        return {
            "product_id": pid,
            "product_name_cn": name_cn,
            "price_current": numbers.get("current_price"),
            "price_change_7d_pct": numbers.get("change_7d_pct", 0),
            "narrative": narrative,
            "chart_stub": chart,
            "forecast_band": numbers.get("forecast_band") if include_forecast else None,
        }

    def _build_summary_recommendations(
        self, sections: list[dict], macro: dict,
    ) -> dict:
        """Build a summary with top-3 recommendations."""
        # Score products by price change magnitude
        top_movers = sorted(sections, key=lambda s: abs(s.get("price_change_7d_pct", 0)), reverse=True)
        recs = []
        for s in top_movers[:3]:
            chg = s.get("price_change_7d_pct", 0)
            action = "hold"
            if chg > 2:
                action = "raise_price"
            elif chg < -2:
                action = "reduce_price"
            recs.append({
                "product_id": s["product_id"],
                "product_name_cn": s["product_name_cn"],
                "change_7d_pct": round(chg, 1),
                "recommended_action": action,
            })
        return {
            "top_movers": [s["product_id"] for s in top_movers[:5]],
            "recommendations": recs,
            "macro_outlook": (
                f"原油{'上涨' if macro.get('crude_change_pct', 0) > 0 else '下跌'}趋势"
                if macro.get("crude_change_pct") else "宏观平稳"
            ),
        }

    @staticmethod
    def _assemble_markdown(
        week_label: str, week_start: str, week_end: str, macro: dict,
        sections: list[dict], summary: dict,
    ) -> str:
        """Assemble the final markdown report."""
        lines = [
            f"# 惠州伊斯科 C5/C9 产品市场周报",
            f"**报告周期**: {week_label} ({week_start} ~ {week_end})",
            "",
            "---",
            "",
            "## 宏观概览",
            f"- 原油: {macro.get('crude_price') or 'N/A'} ({macro.get('crude_change_pct', 0):+.1f}%)",
            f"- 石脑油: {macro.get('naphtha_price') or 'N/A'} ({macro.get('naphtha_change_pct', 0):+.1f}%)",
            "",
            "---",
            "",
        ]
        for s in sections:
            lines.append(f"## {s['product_name_cn']} ({s['product_id']})")
            lines.append(f"本周价格: {s.get('price_current') or 'N/A'} 元/吨 ({s.get('price_change_7d_pct', 0):+.1f}%)")
            lines.append("")
            lines.append(s.get("narrative", ""))
            lines.append("")

        lines.append("---")
        lines.append("## 综合建议")
        for r in summary.get("recommendations", []):
            action_map = {"raise_price": "建议上调", "reduce_price": "建议下调", "hold": "建议维持"}
            lines.append(f"- {r['product_name_cn']}: {action_map.get(r['recommended_action'], '维持')} ({r['change_7d_pct']:+.1f}%)")
        lines.append(f"\n{summary.get('macro_outlook', '')}")

        return "\n".join(lines)


# ── Convenience ──────────────────────────────────────────────

def generate_weekly_report(db=None, report_week: str | None = None) -> dict:
    return WeeklyReportService(db=db).generate_report(report_week=report_week)
