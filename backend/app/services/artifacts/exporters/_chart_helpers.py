"""Pure-Python chart-prep helpers, vendored into the sandbox.

The sandbox runner (``app.services.sandbox.container_manager``) ships a
self-contained copy of ``layout_engine.py`` into ``/input/skill/`` so it can
generate a deck inside the isolation container without the full backend
project on its ``sys.path``. Any ``from app.*`` import at module top of a
vendored file raises ``ModuleNotFoundError: No module named 'app'`` and the
sandbox path silently falls back to the legacy ``create_artifact`` renderer
(producing a bare-bones deck instead of the high-quality Phase1B layout).

So ``layout_engine.py`` imports ``cap_chart_categories`` from THIS module,
not from ``_common.py`` (which carries the heavier ``from app.services.
synexia.contracts import …`` at module top).

In-process renderers (``pptx_export.py`` / ``docx_export.py`` / ``pdf_export
.py`` / ``html_export.py``) keep importing from ``_common.py``; that file
re-exports these names from here for back-compat.

Only stdlib deps: ``re``, ``typing.Any``, ``typing.Optional``. NO ``app.*``.
"""

from __future__ import annotations

import re
from typing import Any, Optional


# Cap enforced at the shared data-prep layer so native charts AND tables agree.
CHART_CATEGORY_CAP = 8
OTHER_BUCKET_LABEL = "Other"


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


__all__ = [
    "CHART_CATEGORY_CAP",
    "OTHER_BUCKET_LABEL",
    "coerce_number",
    "cap_chart_categories",
]