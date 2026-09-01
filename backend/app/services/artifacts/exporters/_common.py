"""Shared helpers for the artifact exporters.

These don't depend on any specific output library — they're pure-Python
formatters and the few string conventions that ALL renderers use
(filename slug, "KPI block" shape, timestamp formatting, …).

Keeping them in one place means a tweak to "KPI value" or "filename slug"
behavior only has to land in one file, not four.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.synexia.contracts import (
    ActionSpec,
    ChartSpec,
    DeckPlan,
    InsightSpec,
    KPISpec,
    ReportCardPayload,
)

# --- Re-exports for back-compat (sandbox-portability refactor 2026-08-19) -----
# The pure-Python chart-prep helpers (``coerce_number``,
# ``CHART_CATEGORY_CAP``, ``OTHER_BUCKET_LABEL``, ``cap_chart_categories``)
# moved to ``_chart_helpers.py`` so the sandbox runner can vendor them
# alongside ``layout_engine.py`` without dragging in the synexia.contracts
# import. In-process renderers (``pptx_export.py`` / ``docx_export.py`` /
# ``pdf_export.py`` / ``html_export.py``) keep importing from ``_common``;
# the shim below preserves their existing import paths. Anything new should
# import from ``_chart_helpers`` directly.
from app.services.artifacts.exporters._chart_helpers import (  # noqa: E402,F401
    CHART_CATEGORY_CAP,
    OTHER_BUCKET_LABEL,
    cap_chart_categories,
    coerce_number,
)


@dataclass
class ExportContext:
    """Side-channel info that the renderers may need (source label, SQL, …).

    Kept deliberately small — anything that's part of the report surface is
    already in the `ReportCardPayload`.  This is just for "footer" /
    "header" text the user can't see in the in-chat card but does want
    in the file (e.g. the SQL that produced the data, the data source
    name, the conversation id for traceability).

    Theme controls (Phase 1 design-engine): `theme` selects a vendored
    theme-system palette name (default ``zhanlu-blue`` reproduces the
    legacy branded deck), `mode` selects light/dark, and `style_recipe`
    selects the Sharp/Soft/Rounded/Pill corner-radius + spacing density.
    Renderers resolve them via ``_theme.load_theme`` /
    ``_theme.resolve_recipe``.
    """

    source: str = ""
    sql: Optional[str] = None
    conversation_id: Optional[str] = None
    user_message: str = ""
    # Phase 1 design-engine
    theme: str = "zhanlu-blue"
    mode: str = "light"
    style_recipe: str = "sharp"
    # The resolved, validate-able theme NAME the deck planner should assume
    # when tuning copy tone/mood (e.g. "zhanlu-blue").  Kept separate from
    # ``theme_tokens`` — a brand kit supplies hex tokens whose name does NOT
    # pass ``_theme.validate_theme_name``, so the planner always receives a
    # vendored base name here while the brand palette arrives via
    # ``theme_tokens``.  Optional and JSON-serializable.
    theme_name: Optional[str] = None
    # Multi-tenant brand kit (SaaS): a flat hex-token dict resolved from
    # the workspace's brand kit by ExportService.  When set, it takes
    # precedence over `theme` (see _theme.resolve_ctx_theme).  Kept as a
    # plain dict so ExportContext stays JSON-serializable.
    theme_tokens: Optional[dict] = None
    # Phase 4 advanced
    cover_art: bool = False
    # Phase C document-type awareness (DOCX renderer):
    #   "report" — full structure: cover page + TOC + all sections (default)
    #   "brief"  — compact: no cover page, no TOC, straight into headings
    #   "memo"   — To/From/Date/Subject header block, no cover, no TOC
    doc_type: str = "report"
    # Deck planner / smart-router (PPT pipeline):
    #   deck_type — data_report | investor_deck | marketing | executive_brief
    #   deck_plan — the planner's structured output (None when the flag is off,
    #               meaning the report-card renderer path is used unchanged).
    deck_type: str = "data_report"
    deck_plan: Optional[DeckPlan] = None
    # Deck skill the agent loaded for this artifact (create_artifact
    # skill=...).  The skill-aware personality layer (skill_deck_profiles)
    # maps it to theme/palette/deck-type defaults so a different skill
    # produces a visibly different deck (2026-08-29).
    skill_name: str = ""


# --- Filename / slug helpers -------------------------------------------------

_FILENAME_BAD = re.compile(r"[^A-Za-z0-9._-]+")


def slugify_title(title: str, *, max_len: int = 60) -> str:
    """Turn a human title into a safe filename stem.

    ASCII letters are lowercased; CJK characters and other alphanumerics
    are kept as-is.  Anything that isn't alphanumeric / dash / dot /
    underscore becomes a dash; runs of dashes collapse.

    >>> slugify_title("Sales report — top materials!")
    'sales-report-top-materials'
    >>> slugify_title("报告 2026 Q1")
    '报告-2026-q1'
    """
    if not title:
        return "report"
    # Replace any non-ASCII / whitespace with a single dash
    s = re.sub(r"\s+", "-", title.strip())
    # Drop non-safe chars (keep Chinese, drop punctuation)
    out = []
    for ch in s:
        if ch.isalnum() or ch in "._-":
            out.append(ch.lower())
        else:
            out.append("-")
    s = "".join(out)
    # Collapse multiple dashes
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        return "report"
    return s[:max_len]


def stamp_filename(title: str, ext: str) -> str:
    """Build a final artifact filename: ``<slug>-YYYYMMDD-HHMM.<ext>``.

    The timestamp makes downloads from the same chat conversation
    non-overwriting on the user's disk.
    """
    slug = slugify_title(title)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    # Strip leading dot from ext if caller passed it
    ext = ext.lstrip(".")
    return f"{slug}-{ts}.{ext}"


# --- Value coercion ----------------------------------------------------------


def coerce_number(v: Any) -> Optional[float]:
    """Best-effort float coercion that accepts strings / ints / floats / None.

    Handles common formatting the LLM emits in chart rows: commas
    (``"1,234.5"``), currency symbols (``"$1,000"``), trailing
    units (``"100%"``, ``"189.3M CNY"``), and a leading minus.

    Returns None if the value can't be parsed — callers should treat
    None as "skip this row in the chart" rather than raising.
    """
    if v is None:
        return None
    if isinstance(v, bool):  # bool is a subclass of int — treat specially
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        # Drop thousands separators and surrounding whitespace.
        s = s.replace(",", "").replace(" ", "")
        # Strip any leading non-numeric, non-sign characters (e.g. "$", "€", "¥", "£").
        s = re.sub(r"^[^0-9eE.\-+]+", "", s)
        # Strip any trailing non-numeric characters (e.g. "%", "CNY", "tons", "USD").
        s = re.sub(r"[^0-9eE.\-+]+$", "", s)
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


# --- Chart-row coercion ------------------------------------------------------


def chart_rows(payload: ReportCardPayload) -> list[dict[str, Any]]:
    """Return the chart's rows, or [] if no chart is present."""
    if payload.chart and payload.chart.data:
        return list(payload.chart.data)
    return []


def chart_x_key(payload: ReportCardPayload) -> str:
    """Return the chart's x-axis column name, or 'label' if no chart."""
    if payload.chart and payload.chart.x_key:
        return payload.chart.x_key
    return "label"


def chart_y_keys(payload: ReportCardPayload) -> list[str]:
    """Return the chart's y-axis column names, or ['value'] if no chart."""
    if payload.chart and payload.chart.y_keys:
        return list(payload.chart.y_keys)
    return ["value"]


def chart_x_value(row: dict[str, Any], payload: ReportCardPayload) -> str:
    """The categorical x-axis label for a chart row."""
    key = chart_x_key(payload)
    v = row.get(key, "")
    if v is None:
        return ""
    return str(v)


# Cap enforced at the shared data-prep layer so native charts AND tables agree.
CHART_CATEGORY_CAP = 8
OTHER_BUCKET_LABEL = "Other"


def cap_chart_categories(
    rows: list[dict[str, Any]],
    x_key: str,
    y_keys: list[str],
    *,
    cap: int = CHART_CATEGORY_CAP,
    other_label: str = OTHER_BUCKET_LABEL,
) -> list[dict[str, Any]]:
    """Cap chart categories at ``cap`` by summed value, aggregating the rest.

    This is the single source of truth for chart/table category limits — both
    the native chart renderer and the table renderer call it, so a deck never
    shows 15 pie slices in the chart while the table lists all 15.

    Ranking is by the *sum* of the numeric y-values per category (so the
    largest contributors stay visible).  Everything beyond ``cap`` is folded
    into one ``other_label`` row whose y-values are the summed remainder.

    Returns aggregated rows (one per distinct category) when there are
    ``<= cap`` distinct categories, or one row per top category plus a single
    ``other_label`` row when there are more, so callers always receive one
    series point per category regardless of how many raw rows shared that
    label.  Returns the rows unchanged when no numeric y-values can be parsed.
    """
    if not rows or not y_keys:
        return rows

    # Aggregate every raw row into one bucket per distinct category label.
    # y-values are summed; the first-seen value is kept for other columns.
    buckets: dict[str, dict[str, Any]] = {}
    totals: dict[str, float] = {}
    for r in rows:
        label = str(r.get(x_key, ""))
        if label not in buckets:
            bucket = {x_key: label}
            for k in y_keys:
                bucket[k] = 0.0
            buckets[label] = bucket
        s = 0.0
        for k in y_keys:
            n = coerce_number(r.get(k))
            if n is not None:
                s += n
        buckets[label][x_key] = label
        for k in y_keys:
            buckets[label][k] = (buckets[label].get(k) or 0.0) + (coerce_number(r.get(k)) or 0.0)
        totals[label] = totals.get(label, 0.0) + s

    distinct = list(buckets.values())
    if len(distinct) <= cap:
        return distinct

    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    top_labels = [label for label, _ in ranked[:cap]]
    other_sum = sum(v for label, v in ranked[cap:])

    capped: list[dict[str, Any]] = [buckets[label] for label in top_labels]
    other_row: dict[str, Any] = {x_key: other_label}
    for k in y_keys:
        other_row[k] = other_sum
    capped.append(other_row)
    return capped


# --- Timestamp / meta helpers ------------------------------------------------


def short_generated_at(payload: ReportCardPayload) -> str:
    """A compact, human-friendly timestamp string for headers/footers."""
    raw = payload.generated_at
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Try ISO 8601 first (the LLM emits "...+00:00" or "Z")
    try:
        cleaned = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(cleaned)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return raw


# --- Top-level text content shared by all renderers --------------------------


def insight_icon_to_emoji(icon: str) -> str:
    """Map the LLM's lucide icon name to a unicode emoji for renderers that
    can't draw lucide icons (CSV doesn't render at all; XLSX/PPTX can use the
    text label; PDF can use either).
    """
    return {
        "lightbulb": "\U0001F4A1",       # 💡
        "trending-up": "\U0001F4C8",     # 📈
        "trending-down": "\U0001F4C9",   # 📉
        "alert-triangle": "\u26A0\uFE0F",  # ⚠️
        "target": "\U0001F3AF",          # 🎯
        "shield-alert": "\U0001F6E1",    # 🛡
        "check": "\u2705",               # ✅
        "check-circle-2": "\u2705",
        "info": "\u2139\uFE0F",          # ℹ️
        "sparkles": "\u2728",            # ✨
        "star": "\u2B50",                # ⭐
        "database": "\U0001F5C4",        # 🗄
        "calendar": "\U0001F4C5",        # 📅
    }.get(icon, "\u2022")                # • fallback


__all__ = [
    "ExportContext",
    "slugify_title",
    "stamp_filename",
    "coerce_number",
    "chart_rows",
    "chart_x_key",
    "chart_y_keys",
    "chart_x_value",
    "cap_chart_categories",
    "CHART_CATEGORY_CAP",
    "OTHER_BUCKET_LABEL",
    "short_generated_at",
    "insight_icon_to_emoji",
]
