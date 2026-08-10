"""Weekly forecast report assembler — reads cached ForecastRun rows and
produces a structured markdown brief.

Pure Python — no LLM, no ML compute.  Deterministic, fast, testable.

Usage::

    from app.services.forecasting.report import WeeklyReportGenerator
    gen = WeeklyReportGenerator(db)
    report = gen.generate(org_id="my-org")
    print(report.markdown)
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.forecasting import ForecastTarget
from app.services.forecasting.engine import ForecastEngine

logger = logging.getLogger(__name__)

_HORIZON_LABEL: dict[str, str] = {
    "3": "3-Day",
    "3d": "3-Day",
    "7": "7-Day",
    "7d": "7-Day",
    "30": "30-Day",
    "30d": "30-Day",
}


def _label(horizon_key: str) -> str:
    """Human-readable label for a horizon key like ``'3'`` or ``'7d'``."""
    return _HORIZON_LABEL.get(horizon_key, f"{horizon_key}-Day")


def _fmt_val(v: Any) -> str:
    """Format a single forecast value for markdown table cells."""
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:,.1f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


# ======================================================================
# Data structures
# ======================================================================


@dataclass
class ProductReport:
    """Per-product section of the weekly report."""

    target_id: str
    product_key: str
    name: str
    quality_grade: Optional[str]
    confidence: Optional[str]
    below_naive_baseline: bool
    results: Optional[dict]  # {horizon_str: {base: [...], bull: [...], bear: [...]}}
    accuracy: list[dict]  # [{horizon_days, mape, naive_mape, skill_vs_naive, ...}]
    model_detail: Optional[dict]
    report_order: Optional[int]

    @property
    def has_forecast(self) -> bool:
        return self.results is not None and len(self.results) > 0

    def _best_accuracy(self, horizon_key: str) -> dict | None:
        """Find the best-matching accuracy log for a given horizon key."""
        try:
            hd = int(horizon_key.replace("d", ""))
        except (ValueError, AttributeError):
            return None
        best = None
        for entry in self.accuracy:
            if entry["horizon_days"] == hd:
                return entry
            if best is None:
                best = entry
        return best

    def _render_scenario_table(self, horizon_key: str) -> str:
        """Render a single-horizon scenario table in markdown."""
        scenario = self.results.get(horizon_key) if self.results else None
        if not scenario or not isinstance(scenario, dict):
            return ""

        horizon_h = horizon_key.replace("d", "")
        label = _label(horizon_key)
        acc = self._best_accuracy(horizon_key)

        lines = [f"### {label} Forecast"]
        if acc:
            mape_s = f"{acc['mape']:.1%}" if acc.get("mape") is not None else "N/A"
            lines.append(f"MAPE: {mape_s}  |  Skill vs naive: {acc.get('skill_vs_naive', 'N/A')}")

        # Determine scenario order and values
        rows = [("base", scenario.get("base")), ("bull", scenario.get("bull")), ("bear", scenario.get("bear"))]

        # Collect all non-None value series
        series_data = [(s_name, vals) for s_name, vals in rows if vals is not None]

        if not series_data:
            lines.append("_No scenario data available._")
            return "\n".join(lines)

        # Check if values are lists (multi-step) or scalars
        first_vals = series_data[0][1]
        if isinstance(first_vals, list):
            # Multi-step: each horizon day is a column
            n_steps = len(first_vals)
            header = "| Scenario | " + " | ".join(f"Day {i + 1}" for i in range(n_steps)) + " |"
            sep = "|" + "|".join(" --- " for _ in range(n_steps + 1)) + "|"
            data_rows = []
            for s_name, vals in series_data:
                if isinstance(vals, list) and len(vals) == n_steps:
                    data_rows.append("| " + s_name.capitalize() + " | " + " | ".join(_fmt_val(v) for v in vals) + " |")
            if not data_rows:
                return "\n".join(lines)
            lines.extend([header, sep] + data_rows)
        else:
            # Scalar: single value per scenario
            lines.append("| Scenario | Value |")
            lines.append("| --- | --- |")
            for s_name, vals in series_data:
                lines.append(f"| {s_name.capitalize()} | {_fmt_val(vals)} |")

        return "\n".join(lines)

    @property
    def markdown_section(self) -> str:
        """Pre-rendered markdown for this product."""
        parts: list[str] = []

        # ── Header with grade and confidence ─────────────────────
        grade_str = f"[Grade: {self.quality_grade}]" if self.quality_grade else ""
        conf_str = f"[Confidence: {self.confidence}]" if self.confidence else ""
        header_meta = "  ".join(filter(None, [grade_str, conf_str]))
        parts.append(f"## {self.name}  {header_meta}".rstrip())

        # ── Honesty gate ─────────────────────────────────────────
        if self.below_naive_baseline:
            parts.append(
                "> **⚠️  WARNING: Below naive baseline.**  "
                "This forecast failed to outperform the seasonal-naive baseline.  "
                "Published values fall back to naive.  Treat with caution."
            )
        else:
            parts.append(
                "> **Honesty gate: PASS** — ensemble outperforms naive baseline."
            )

        # ── Accuracy summary ────────────────────────────────────
        if self.accuracy:
            parts.append("**Accuracy (per horizon):**\n")
            parts.append("| Horizon | MAPE | Naive MAPE | Skill |")
            parts.append("| --- | --- | --- | --- |")
            for entry in self.accuracy:
                m = f"{entry['mape']:.1%}" if entry.get("mape") is not None else "N/A"
                n = f"{entry['naive_mape']:.1%}" if entry.get("naive_mape") is not None else "N/A"
                s = f"{entry['skill_vs_naive']:.3f}" if entry.get("skill_vs_naive") is not None else "N/A"
                parts.append(f"| {entry['horizon_days']} days | {m} | {n} | {s} |")
            parts.append("")

        # ── No forecast guard ───────────────────────────────────
        if not self.has_forecast:
            parts.append("**⚠️  No forecast available** — run `forecast_run` first.\n")
            return "\n\n".join(parts)

        # ── Scenario tables (sorted by horizon) ────────────────
        horizon_keys = sorted(self.results.keys(), key=lambda k: int(k.replace("d", "")) if k.replace("d", "").isdigit() else 999)
        for hk in horizon_keys:
            table = self._render_scenario_table(hk)
            if table:
                parts.append(table)

        # ── Model detail footer ─────────────────────────────────
        if self.model_detail:
            parts.append("#### Model Detail")
            models_run = self.model_detail.get("models_run", [])
            weights = self.model_detail.get("weights", {})
            failed = self.model_detail.get("failed", [])
            if models_run:
                parts.append(f"- Models run: {', '.join(models_run)}")
            if weights:
                w_str = ", ".join(f"{k}: {v:.2f}" for k, v in weights.items())
                parts.append(f"- Ensemble weights: {w_str}")
            if failed:
                parts.append(f"- Models failed: {', '.join(failed)}")

        return "\n\n".join(parts)


@dataclass
class WeeklyReport:
    """Top-level weekly forecast brief."""

    generated_at: datetime
    as_of_date: Optional[datetime]
    org_id: str
    summary: dict  # {total, below_baseline, confidence_dist: {high, medium, low, none}}
    products: list[ProductReport]

    @property
    def markdown(self) -> str:
        """Full markdown document."""
        parts: list[str] = []

        # ── Title and generation metadata ───────────────────────
        date_str = self.as_of_date.strftime("%Y-%m-%d") if self.as_of_date else self.generated_at.strftime("%Y-%m-%d")
        parts.append(f"# Weekly Forecast Brief — {date_str}")
        parts.append("")

        # ── Executive summary ──────────────────────────────────
        total = self.summary.get("total", 0)
        below = self.summary.get("below_baseline", 0)
        cdist = self.summary.get("confidence_dist", {})

        parts.append("## Executive Summary")
        parts.append(f"- **{total}** products tracked")
        parts.append(f"- **{below}** forecasts below naive baseline (honesty gate)")
        parts.append(f"- Confidence: high={cdist.get('high', 0)}, medium={cdist.get('medium', 0)}, "
                     f"low={cdist.get('low', 0)}, none={cdist.get('none', 0)}")
        parts.append(f"- Generated at {self.generated_at.isoformat(timespec='seconds')}")
        parts.append("")

        # ── Per-product sections ───────────────────────────────
        for product in self.products:
            parts.append(product.markdown_section)
            parts.append("")  # blank line separator

        return "\n".join(parts)


# ======================================================================
# Generator
# ======================================================================


class WeeklyReportGenerator:
    """Assemble a weekly forecast report from cached ForecastRun rows.

    Pure read — no ML, no LLM, no DB writes.  Fast and deterministic.
    """

    def __init__(self, db: Session):
        self._db = db

    def generate(self, org_id: str, app_id: str = "default-app") -> WeeklyReport:
        """Query all ``include_in_weekly_report`` targets, fetch cached
        forecasts + accuracy, and assemble a ``WeeklyReport``.

        Targets are ordered by ``report_order`` then ``name``.  Targets
        with no cached forecast are included with a warning section.
        """
        engine = ForecastEngine(self._db)

        # ── Fetch targets ────────────────────────────────────────
        targets = (
            self._db.query(ForecastTarget)
            .filter(
                ForecastTarget.org_id == org_id,
                ForecastTarget.include_in_weekly_report == True,
                ForecastTarget.is_deleted == False,
            )
            .order_by(
                ForecastTarget.report_order.asc().nulls_last(),
                ForecastTarget.name.asc(),
            )
            .all()
        )

        if not targets:
            logger.info("Weekly report: no targets with include_in_weekly_report=True for org=%s", org_id)

        # ── Build per-product reports ────────────────────────────
        products: list[ProductReport] = []
        below_count = 0
        confidence_counts: Counter = Counter()

        for target in targets:
            forecast = engine.get_forecast(target.id)
            accuracy = engine.get_accuracy(target.id)

            below = forecast.get("below_naive_baseline", False) if forecast else False
            confidence = forecast.get("confidence") if forecast else None

            if below:
                below_count += 1
            confidence_counts[confidence or "none"] += 1

            products.append(
                ProductReport(
                    target_id=target.id,
                    product_key=target.product_key,
                    name=target.name,
                    quality_grade=target.quality_grade,
                    confidence=confidence,
                    below_naive_baseline=below,
                    results=forecast.get("results") if forecast else None,
                    accuracy=accuracy,
                    model_detail=forecast.get("model_detail") if forecast else None,
                    report_order=target.report_order,
                )
            )

        # ── Assemble summary ─────────────────────────────────────
        as_of_date: Optional[datetime] = None
        for p in products:
            if p.has_forecast and forecast:
                # Use the as_of_date from the forecast dict
                aod = forecast.get("as_of_date")
                if aod and (as_of_date is None or aod > as_of_date):
                    as_of_date = aod if isinstance(aod, datetime) else datetime.fromisoformat(aod)

        summary = {
            "total": len(products),
            "below_baseline": below_count,
            "confidence_dist": {
                "high": confidence_counts.get("high", 0),
                "medium": confidence_counts.get("medium", 0),
                "low": confidence_counts.get("low", 0),
                "none": confidence_counts.get("none", 0),
            },
        }

        return WeeklyReport(
            generated_at=datetime.now(timezone.utc),
            as_of_date=as_of_date,
            org_id=org_id,
            summary=summary,
            products=products,
        )
