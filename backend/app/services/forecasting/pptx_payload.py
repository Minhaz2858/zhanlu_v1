"""Forecast-to-ReportCard payload assembler.

Converts a ``WeeklyReport`` (Section 4) into a ``ReportCardPayload``
consumable by ``pptx_export.render()``. Pure Python — no LLM, no ML.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.forecasting.report import ProductReport, WeeklyReport
from app.services.synexia.contracts import (
    ChartSpec,
    InsightSpec,
    KPISpec,
    ReportCardPayload,
    SectionSpec,
)

_HORIZON_DAYS: dict[str, int] = {"3": 3, "7": 7, "30": 30}
_HORIZON_LABEL: dict[str, str] = {"3": "3-Day", "7": "7-Day", "30": "30-Day"}


class ForecastPayloadAssembler:
    """Convert a ``WeeklyReport`` into a ``ReportCardPayload``."""

    def __init__(self, db: Session):
        self._db = db

    def assemble(
        self,
        report: WeeklyReport,
        org_id: str,
        target_id: str | None = None,
        horizon: str = "7",
    ) -> ReportCardPayload:
        """Assemble a forecast-themed ReportCardPayload.

        Args:
            report: Pre-built WeeklyReport.
            org_id: Organization id.
            target_id: Optional specific product to chart. Falls back
                       to the first product with a forecast.
            horizon: Forecast horizon ("3", "7", "30").
        """
        featured = self._pick_product(report, target_id)
        date_str = (
            report.as_of_date.strftime("%Y-%m-%d")
            if report.as_of_date
            else report.generated_at.strftime("%Y-%m-%d")
        )
        kf = self._build_key_findings(report)

        return ReportCardPayload(
            title=f"Weekly Forecast Brief — {date_str}",
            source=f"Zhanlu Forecasting Engine · org={org_id}",
            generated_at=report.generated_at.isoformat(timespec="seconds"),
            summary=self._build_summary(report),
            kpis=self._build_kpis(report),
            chart=self._build_chart(featured, horizon),
            insights=kf,
            user_signal="export",
            warnings=self._build_warnings(report),
            methodology=self._build_methodology(report),
            key_findings=kf,
            recommendations=self._build_recommendations(report),
            sections=self._build_sections(report),
            sql="",
        )

    # -- helpers ----------------------------------------------------------

    def _pick_product(
        self, report: WeeklyReport, target_id: str | None
    ) -> ProductReport | None:
        if target_id:
            for p in report.products:
                if p.target_id == target_id:
                    return p
        for p in report.products:
            if p.has_forecast:
                return p
        return report.products[0] if report.products else None

    def _build_summary(self, report: WeeklyReport) -> str:
        total = report.summary.get("total", 0)
        below = report.summary.get("below_baseline", 0)
        cdist = report.summary.get("confidence_dist", {})
        parts = [f"Weekly forecast review covering {total} products."]
        if below > 0:
            parts.append(
                f"{below} product(s) fell below the naive baseline "
                f"and have been flagged for review."
            )
        else:
            parts.append("All forecasts pass the honesty gate.")
        conf_parts = []
        for level in ("high", "medium", "low"):
            c = cdist.get(level, 0)
            if c:
                conf_parts.append(f"{c} {level}")
        if conf_parts:
            parts.append(f"Confidence distribution: {', '.join(conf_parts)}.")
        return " ".join(parts)

    def _build_kpis(self, report: WeeklyReport) -> list[KPISpec]:
        total = report.summary.get("total", 0)
        below = report.summary.get("below_baseline", 0)
        cdist = report.summary.get("confidence_dist", {})
        kpis = [
            KPISpec(label="Products Tracked", value=str(total),
                    caption="include_in_weekly_report=True"),
        ]
        if below > 0:
            kpis.append(KPISpec(label="Below Baseline", value=str(below),
                                delta=f"{below} need review",
                                caption="Honesty gate failures"))
        else:
            kpis.append(KPISpec(label="Below Baseline", value="0",
                                delta="All pass", caption="Honesty gate clean"))
        kpis.append(KPISpec(label="High Confidence",
                            value=str(cdist.get("high", 0)),
                            caption="Ensemble confidence level"))
        total_skill, skill_count = 0.0, 0
        for p in report.products:
            if p.accuracy:
                svn = p.accuracy[0].get("skill_vs_naive")
                if svn is not None:
                    total_skill += float(svn)
                    skill_count += 1
        if skill_count > 0:
            kpis.append(KPISpec(
                label="Avg Skill vs Naive", value=f"{total_skill / skill_count:.3f}",
                delta=f"Across {skill_count} products",
                caption="Ensemble skill score"))
        return kpis

    def _build_chart(
        self, featured: ProductReport | None, horizon: str
    ) -> ChartSpec | None:
        if not featured or not featured.has_forecast or not featured.results:
            return None
        scenario = featured.results.get(horizon)
        if not scenario or not isinstance(scenario, dict):
            return None
        base_vals = scenario.get("base")
        bull_vals = scenario.get("bull")
        bear_vals = scenario.get("bear")
        if isinstance(base_vals, list):
            n = len(base_vals)
            data: list[dict[str, Any]] = []
            for i in range(n):
                row: dict[str, Any] = {"day": i + 1}
                if isinstance(base_vals, list) and i < len(base_vals):
                    row["base"] = base_vals[i]
                if isinstance(bull_vals, list) and i < len(bull_vals):
                    row["bull"] = bull_vals[i]
                if isinstance(bear_vals, list) and i < len(bear_vals):
                    row["bear"] = bear_vals[i]
                data.append(row)
        elif base_vals is not None:
            data = [{
                "day": 1, "base": base_vals,
                "bull": bull_vals if bull_vals is not None else base_vals,
                "bear": bear_vals if bear_vals is not None else base_vals,
            }]
        else:
            return None
        label = _HORIZON_LABEL.get(horizon, f"{horizon}-Day")
        return ChartSpec(
            type="line",
            title=f"{featured.name} — {label} Scenario Forecast",
            x_key="day", y_keys=["base", "bull", "bear"], data=data,
        )

    def _build_warnings(self, report: WeeklyReport) -> list[str]:
        return [
            f"⚠ {p.name}: Forecast failed honesty gate — below naive baseline. "
            f"Published values fall back to naive. Review recommended."
            for p in report.products if p.below_naive_baseline
        ]

    def _build_methodology(self, report: WeeklyReport) -> str:
        as_of = (
            report.as_of_date.strftime("%Y-%m-%d")
            if report.as_of_date else "N/A"
        )
        return (
            f"Forecasts generated by the Zhanlu ensemble forecasting engine "
            f"(as of {as_of}). Each target is fitted with multiple models "
            f"(SeasonalNaive, Statistical, ML) and combined via a weighted "
            f"ensemble. The honesty gate compares ensemble performance against "
            f"a seasonal-naive baseline on backtest data; when the ensemble "
            f"underperforms, published forecasts fall back to naive to avoid "
            f"over-confidence. Accuracy metrics (MAPE, Skill vs Naive) are "
            f"computed per horizon from a rolling backtest."
        )

    def _build_key_findings(self, report: WeeklyReport) -> list[InsightSpec]:
        findings: list[InsightSpec] = []
        for p in report.products:
            icon = "alert-triangle" if p.below_naive_baseline else "trending-up"
            conf = p.confidence or "unknown"
            if not p.has_forecast:
                findings.append(InsightSpec(
                    icon="alert-triangle",
                    text=f"{p.name}: No forecast available — run forecast_run first."))
                continue
            base_val = None
            if p.results:
                for hk in ("7", "3", "30"):
                    sc = p.results.get(hk)
                    if isinstance(sc, dict):
                        b = sc.get("base")
                        if isinstance(b, list) and b:
                            base_val = b[0]; break
                        elif b is not None:
                            base_val = b; break
            tag = " ⚠ below naive" if p.below_naive_baseline else ""
            if base_val is not None:
                vs = f"{base_val:,.1f}" if isinstance(base_val, (int, float)) else str(base_val)
                findings.append(InsightSpec(icon=icon,
                    text=f"{p.name}: 7-day base forecast = {vs} (confidence: {conf}){tag}"))
            else:
                findings.append(InsightSpec(icon=icon,
                    text=f"{p.name}: Forecast available (confidence: {conf}){tag}"))
        if not findings:
            findings.append(InsightSpec(icon="info",
                text="No products configured for weekly forecasting."))
        return findings

    def _build_recommendations(self, report: WeeklyReport) -> list[InsightSpec]:
        recs: list[InsightSpec] = []
        for p in report.products:
            if p.below_naive_baseline:
                recs.append(InsightSpec(icon="alert-triangle",
                    text=(
                        f"Review {p.name}: Forecast underperforms naive baseline. "
                        f"Consider checking data quality, adding causal drivers, "
                        f"or increasing history length.")))
        if not recs:
            recs.append(InsightSpec(icon="check-circle",
                text="All products pass the honesty gate. No review actions required."))
        return recs

    def _build_sections(self, report: WeeklyReport) -> list[SectionSpec]:
        sections: list[SectionSpec] = []
        table_md = self._render_forecast_table(report)
        if table_md:
            sections.append(SectionSpec(
                title="Forecast Summary", content=table_md, type="findings"))
        accuracy_md = self._render_accuracy_section(report)
        if accuracy_md:
            sections.append(SectionSpec(
                title="Accuracy Metrics", content=accuracy_md, type="methodology"))
        return sections

    def _render_forecast_table(self, report: WeeklyReport) -> str:
        if not report.products:
            return ""
        all_horizons: set[str] = set()
        for p in report.products:
            if p.results:
                all_horizons.update(p.results.keys())
        if not all_horizons:
            return "_No forecast data available._"
        sorted_h = sorted(
            all_horizons,
            key=lambda k: _HORIZON_DAYS.get(k, 999),
        )
        lines = [
            "| Product | " + " | ".join(_HORIZON_LABEL.get(h, h) for h in sorted_h) + " | Confidence |",
            "|" + "|".join(" --- " for _ in range(len(sorted_h) + 2)) + "|",
        ]
        for p in report.products:
            prefix = "⚠ " if p.below_naive_baseline else ""
            cells = [f"{prefix}{p.name}"]
            for hk in sorted_h:
                sc = p.results.get(hk) if p.results else None
                if isinstance(sc, dict):
                    b = sc.get("base")
                    if isinstance(b, list) and b:
                        cells.append(f"{b[0]:,.1f}" if isinstance(b[0], (int, float)) else str(b[0]))
                    elif b is not None:
                        cells.append(f"{b:,.1f}" if isinstance(b, (int, float)) else str(b))
                    else:
                        cells.append("N/A")
                else:
                    cells.append("N/A")
            cells.append(p.confidence or "N/A")
            lines.append("| " + " | ".join(cells) + " |")
        if any(p.below_naive_baseline for p in report.products):
            lines.append("\n⚠ = Below naive baseline (honesty gate failure)")
        return "\n".join(lines)

    def _render_accuracy_section(self, report: WeeklyReport) -> str:
        products_with_acc = [p for p in report.products if p.accuracy]
        if not products_with_acc:
            return ""
        lines = [
            "| Product | Horizon | MAPE | Naive MAPE | Skill vs Naive |",
            "| --- | --- | --- | --- | --- |",
        ]
        for p in products_with_acc:
            for entry in p.accuracy:
                m = f"{entry['mape']:.1%}" if entry.get("mape") is not None else "N/A"
                n = f"{entry['naive_mape']:.1%}" if entry.get("naive_mape") is not None else "N/A"
                s = f"{entry['skill_vs_naive']:.3f}" if entry.get("skill_vs_naive") is not None else "N/A"
                hd = entry.get("horizon_days", "?")
                lines.append(f"| {p.name} | {hd} days | {m} | {n} | {s} |")
        return "\n".join(lines)
