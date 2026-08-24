"""Pre-aggregation engine for CEO-grade report synthesis.

Modern AI agents (Claude, Kimi, MiniMax) don't dump raw rows at the LLM and
hope it does arithmetic in its head — they pre-compute the aggregates first,
then hand the LLM concrete numbers to INTERPRET.  This module does exactly
that: it converts a raw row snapshot into a compact, deterministic
``AggregationResult`` with per-column stats, group-by breakdowns, period
comparisons, concentration metrics, and anomaly flags.

The result is rendered as a structured text block and injected into the
synthesis LLM prompt (see ``report_synthesis.py``), giving the synthesizer
hard numbers for the executive summary, KPI table, and breakdowns without
needing to recompute anything from raw JSON.

Design constraints:
- Pure Python, no LLM, no new dependencies (reuses ``data_profiler``).
- Fully generic — column names come from the data, never hardcoded.
- Deterministic ordering (sort group-by by aggregated value desc).
- Cheap: single O(n) pass over rows for column stats + group-bys.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Optional

from app.services.artifacts.data_profiler import (
    _to_float,
    detect_column_type,
    group_by_profile,
)

# Maximum distinct groups kept per dimension (top-N by aggregated value).
_TOP_N = 10

# A column with more distinct values than this is a "high-cardinality" key
# (likely an ID or a free-text field) — excluded from group-by breakdowns.
_MAX_GROUP_BY_CARDINALITY = 60

# Minimum distinct values for a categorical column to count as a dimension.
_MIN_DIMENSION_CARDINALITY = 2


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ColumnStats:
    """Per-column summary statistics."""

    col_type: str  # "numeric" | "categorical" | "temporal" | "empty"
    non_null: int = 0
    null_pct: float = 0.0
    distinct: int = 0
    sum: float | None = None
    avg: float | None = None
    min: float | None = None
    max: float | None = None
    std: float | None = None

    def to_text(self, col: str) -> str:
        parts = [f"{col} ({self.col_type})"]
        if self.non_null:
            parts.append(f"non-null={self.non_null}")
        if self.null_pct > 0:
            parts.append(f"null={self.null_pct:.0%}")
        if self.col_type == "numeric" and self.sum is not None:
            parts.append(f"sum={self.sum:,.2f}")
        if self.avg is not None:
            parts.append(f"avg={self.avg:,.2f}")
        if self.min is not None and self.max is not None:
            parts.append(f"range=[{self.min:,.2f}..{self.max:,.2f}]")
        return "; ".join(parts)


@dataclass
class GroupByBreakdown:
    """Top-N groups for one dimension, with aggregated numeric values."""

    dimension: str
    value_col: str
    groups: list[dict[str, Any]] = field(default_factory=list)  # [{group, value, share_pct}]
    total: float = 0.0
    top3_share: float | None = None

    def to_text(self) -> str:
        if not self.groups:
            return f"[{self.dimension} × {self.value_col}]: no groups"
        lines = [f"[{self.dimension} × {self.value_col}] (top {len(self.groups)} of {len(self.groups)}):"]
        for g in self.groups:
            share = g.get("share_pct")
            share_txt = f", {share:.1f}%" if share is not None else ""
            lines.append(f"  {g['group']}: {g['value']:,.2f}{share_txt}")
        if self.top3_share is not None:
            lines.append(f"  top-3 concentration: {self.top3_share:.1f}%")
        return "\n".join(lines)


@dataclass
class PeriodComparison:
    """Current vs previous period comparison for the primary date column."""

    date_col: str
    current_period: str
    previous_period: str
    current_total: float
    previous_total: float
    change_pct: float | None

    def to_text(self) -> str:
        if self.change_pct is None:
            change_txt = "n/a (no previous period data)"
        else:
            change_txt = f"{self.change_pct:+.1f}%"
        return (
            f"[period comparison on {self.date_col}]: {self.current_period} = "
            f"{self.current_total:,.2f} vs {self.previous_period} = {self.previous_total:,.2f} "
            f"(change {change_txt})"
        )


@dataclass
class ConcentrationMetrics:
    """Concentration of the primary numeric column across top groups."""

    value_col: str
    top3_share: float | None
    hhi: float | None  # Herfindahl-Hirschman Index (0..1 normalized)

    def to_text(self) -> str:
        parts = [f"[concentration of {self.value_col}]"]
        if self.top3_share is not None:
            parts.append(f"top-3 share={self.top3_share:.1f}%")
        if self.hhi is not None:
            parts.append(f"HHI={self.hhi:.3f}")
        return "; ".join(parts)


@dataclass
class AggregationResult:
    """Complete pre-aggregation of a raw row snapshot."""

    total_rows: int = 0
    column_profile: dict[str, ColumnStats] = field(default_factory=dict)
    numeric_columns: list[str] = field(default_factory=list)
    categorical_columns: list[str] = field(default_factory=list)
    temporal_columns: list[str] = field(default_factory=list)
    dimensions_detected: list[str] = field(default_factory=list)
    group_by_breakdowns: list[GroupByBreakdown] = field(default_factory=list)
    period_comparison: Optional[PeriodComparison] = None
    concentration: list[ConcentrationMetrics] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    def to_prompt_block(self, max_breakdowns: int = 6, max_groups_per_breakdown: int = 8) -> str:
        """Render the aggregation as a compact, LLM-friendly text block.

        This block is injected into the synthesis prompt BEFORE the raw rows
        so the synthesizer has concrete numbers to interpret.  ``max_*``
        caps keep the block well under the context budget.
        """
        if self.total_rows == 0:
            return "PRE-COMPUTED ANALYSIS: empty snapshot (0 rows).\n"

        lines = [
            "─── PRE-COMPUTED ANALYSIS (aggregates computed from ALL rows; interpret, don't recompute) ───",
            f"Total rows: {self.total_rows}",
        ]

        # Per-column stats (cap to avoid noise on very wide tables).
        for col, stats in list(self.column_profile.items())[:15]:
            lines.append(f"  {stats.to_text(col)}")

        # Period comparison (MoM) if detected.
        if self.period_comparison:
            lines.append(f"  {self.period_comparison.to_text()}")

        # Concentration metrics.
        for c in self.concentration[:3]:
            lines.append(f"  {c.to_text()}")

        # Group-by breakdowns (top-N per dimension).
        breakdowns = self.group_by_breakdowns[:max_breakdowns]
        if breakdowns:
            lines.append("Breakdowns (top groups by value):")
            for bd in breakdowns:
                # Re-render with capped groups.
                groups_txt = ", ".join(
                    f"{g['group']}={g['value']:,.0f}({g.get('share_pct', 0):.0f}%)"
                    for g in bd.groups[:max_groups_per_breakdown]
                )
                lines.append(f"  {bd.dimension} (by {bd.value_col}): {groups_txt}")

        # Anomalies.
        if self.anomalies:
            lines.append("Anomalies / data-quality flags:")
            for a in self.anomalies[:8]:
                lines.append(f"  ⚠ {a}")

        lines.append("─── END PRE-COMPUTED ANALYSIS ───")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _column_values(rows: list[dict], col: str) -> list[Any]:
    return [r.get(col) for r in rows]


def _compute_column_stats(rows: list[dict], col: str) -> ColumnStats:
    vals = _column_values(rows, col)
    non_null_vals = [v for v in vals if v is not None and v != ""]
    total = len(vals)
    non_null = len(non_null_vals)
    null_pct = 0.0 if not total else 1.0 - non_null / total
    distinct = len({v for v in non_null_vals})

    col_type = detect_column_type(non_null_vals)
    stats = ColumnStats(col_type=col_type, non_null=non_null, null_pct=null_pct, distinct=distinct)

    if col_type == "numeric":
        floats = [_to_float(v) for v in non_null_vals]
        floats = [f for f in floats if f is not None]
        if floats:
            stats.sum = sum(floats)
            stats.avg = statistics.mean(floats)
            stats.min = min(floats)
            stats.max = max(floats)
            if len(floats) > 1:
                stats.std = statistics.pstdev(floats) if len(floats) < 30 else statistics.stdev(floats)
    return stats


def _detect_period_boundary(rows: list[dict], date_col: str) -> tuple[str, str] | None:
    """Split rows into current/previous period based on the latest date.

    Handles common period formats: YYYY-MM-DD, YYYY-MM, YYYY/MM/DD, and
    ISO timestamps.  Returns ``(current_label, previous_label)`` where the
    labels describe the boundary (e.g. "2026-07" vs "2026-06").
    """
    values = [r.get(date_col) for r in rows if r.get(date_col)]
    if not values:
        return None
    try:
        parsed = sorted(set(str(v)[:10] for v in values))
    except Exception:
        return None
    if len(parsed) < 2:
        return None

    # Take the most recent date as the current period anchor.
    latest = parsed[-1]
    if len(latest) >= 7 and latest[4] in ("-", "/"):
        try:
            year = int(latest[:4])
            month = int(latest[5:7])
            prev_year = year - 1 if month == 1 else year
            prev_month = 12 if month == 1 else month - 1
            current_label = f"{year:04d}-{month:02d}"
            previous_label = f"{prev_year:04d}-{prev_month:02d}"
            return current_label, previous_label
        except (ValueError, IndexError):
            return None
    return None


def _period_value(rows: list[dict], date_col: str, period_label: str, value_col: str) -> float:
    """Sum the given numeric value column for rows in the YYYY-MM period."""
    total = 0.0
    for r in rows:
        raw = str(r.get(date_col) or "")[:7]
        if raw != period_label:
            continue
        f = _to_float(r.get(value_col))
        if f is not None:
            total += f
    return total


def _compute_hhi(rows: list[dict], dimension: str, value_col: str) -> float | None:
    """Herfindahl-Hirschman Index (normalized 0..1) for a dimension."""
    groups: dict[str, float] = {}
    for r in rows:
        key = r.get(dimension)
        val = _to_float(r.get(value_col))
        if key is None or val is None:
            continue
        groups[str(key)] = groups.get(str(key), 0.0) + val
    total = sum(groups.values())
    if not total:
        return None
    hhi = sum((v / total) ** 2 for v in groups.values())
    # Normalize to 0..1 for interpretability.
    n = len(groups)
    if n <= 1:
        return 1.0
    return (hhi - 1 / n) / (1 - 1 / n)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def pre_aggregate(rows: list[dict]) -> AggregationResult:
    """Compute a full aggregation result from raw rows.

    Args:
        rows: Raw row dicts from the data agent.

    Returns:
        AggregationResult with column stats, group-by breakdowns, period
        comparison, concentration metrics, and anomaly flags.
    """
    rows = rows or []
    result = AggregationResult(total_rows=len(rows))
    if not rows:
        return result

    columns = list(rows[0].keys())

    # ── Column categorization ──────────────────────────────────────────
    for col in columns:
        stats = _compute_column_stats(rows, col)
        result.column_profile[col] = stats
        if stats.col_type == "numeric":
            result.numeric_columns.append(col)
        elif stats.col_type == "categorical":
            result.categorical_columns.append(col)
        elif stats.col_type == "temporal":
            result.temporal_columns.append(col)

    # ── Group-by breakdowns (top-N per categorical dimension) ──────────
    for dim in result.categorical_columns:
        stats = result.column_profile[dim]
        if stats.distinct < _MIN_DIMENSION_CARDINALITY or stats.distinct > _MAX_GROUP_BY_CARDINALITY:
            continue
        # Choose the most important numeric column for this breakdown:
        # prefer a column named like revenue/amount/qty, else the first.
        value_col = _pick_primary_numeric(result.numeric_columns, dim)
        if not value_col:
            continue

        top_groups = group_by_profile(rows, dim, value_col, top_n=_TOP_N)
        if not top_groups:
            continue
        # Recompute total over ALL groups (not just top-N) for accurate shares.
        all_groups = group_by_profile(rows, dim, value_col, top_n=None)
        grand_total = sum(g.get("value", 0.0) for g in all_groups)
        breakdown = GroupByBreakdown(
            dimension=dim,
            value_col=value_col,
            groups=top_groups,
            total=grand_total,
        )
        if len(all_groups) >= 3:
            top3 = sum(g.get("value", 0.0) for g in all_groups[:3])
            breakdown.top3_share = top3 / grand_total * 100 if grand_total else None
        result.group_by_breakdowns.append(breakdown)
        result.dimensions_detected.append(dim)

    # ── Period comparison (MoM) on the primary date column ─────────────
    if result.temporal_columns:
        primary_date = result.temporal_columns[0]
        primary_num = _pick_primary_numeric(result.numeric_columns, None)
        boundary = _detect_period_boundary(rows, primary_date)
        if boundary:
            current_label, previous_label = boundary
            current_total = (
                _period_value(rows, primary_date, current_label, primary_num)
                if primary_num else 0.0
            )
            previous_total = (
                _period_value(rows, primary_date, previous_label, primary_num)
                if primary_num else 0.0
            )
            change_pct = None
            if previous_total:
                change_pct = (current_total - previous_total) / previous_total * 100
            result.period_comparison = PeriodComparison(
                date_col=primary_date,
                current_period=current_label,
                previous_period=previous_label,
                current_total=current_total,
                previous_total=previous_total,
                change_pct=change_pct,
            )

    # ── Concentration metrics for the primary numeric column ───────────
    primary_num = _pick_primary_numeric(result.numeric_columns, None)
    if primary_num and result.dimensions_detected:
        for dim in result.dimensions_detected[:2]:
            hhi = _compute_hhi(rows, dim, primary_num)
            top3_share = None
            # Reuse the group-by breakdown if available.
            for bd in result.group_by_breakdowns:
                if bd.dimension == dim and bd.value_col == primary_num:
                    top3_share = bd.top3_share
                    break
            if hhi is not None or top3_share is not None:
                result.concentration.append(
                    ConcentrationMetrics(value_col=primary_num, top3_share=top3_share, hhi=hhi)
                )

    # ── Anomaly flags ──────────────────────────────────────────────────
    for col in result.numeric_columns:
        stats = result.column_profile[col]
        if stats.null_pct > 0.3 and stats.non_null > 0:
            result.anomalies.append(f"{col} has {stats.null_pct:.0%} nulls")
        if stats.sum is not None and stats.sum < 0 and stats.non_null > 0:
            result.anomalies.append(f"{col} has negative totals (sum={stats.sum:,.2f})")
        # Outlier detection: values beyond 3σ from mean.
        if stats.avg is not None and stats.std and stats.std > 0:
            outliers = 0
            for r in rows:
                f = _to_float(r.get(col))
                if f is not None and abs(f - stats.avg) > 3 * stats.std:
                    outliers += 1
            if outliers and outliers >= max(1, int(len(rows) * 0.05)):
                result.anomalies.append(
                    f"{col} has {outliers} outlier rows (>3σ from mean {stats.avg:,.2f})"
                )

    return result


def _pick_primary_numeric(numeric_cols: list[str], exclude_dim: str | None) -> str | None:
    """Pick the most meaningful numeric column for aggregation.

    Prefers columns whose names contain revenue/amount/sales/qty/value
    keywords; falls back to the first numeric column.
    """
    if not numeric_cols:
        return None
    priority = ("revenue", "amount", "sales", "qty", "quantity", "value", "total", "price", "cost")
    best = None
    best_rank = len(priority) + 1
    for col in numeric_cols:
        lower = col.lower().replace("_", "").replace(" ", "")
        rank = next((i for i, kw in enumerate(priority) if kw in lower), len(priority) + 1)
        if rank < best_rank:
            best_rank = rank
            best = col
    return best
