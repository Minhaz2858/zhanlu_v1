"""Chart suggestor — OCI online pipeline "Visualization" stage (P2-7).

The reference pipeline ends with a Visualization step before the response. The
answer payload therefore carries a `visualization` field: a DETERMINISTIC
chart-type suggestion inferred from the shape of the result (column types +
cardinality), never from another LLM call.

Rules this module never breaks:

* **Deterministic and pure.** Same rows in, same suggestion out. No LLM, no
  I/O, no exceptions on odd data — odd data just degrades to "table".
* **Advisory.** The suggestion is a hint for the frontend renderer; callers
  that already decided a chart (dashboards, artifacts) ignore it.
* **Bounded payload.** The suggestion never copies the rows; it names columns.

Shape grammar (first match wins):
- 1 temporal + 1-3 numeric        -> line (trend over time)
- 1 categorical + 1 numeric       -> bar (top-N by value, desc)
- 1 categorical + 2-4 numeric     -> grouped bar
- 2 categorical + 1 numeric       -> stacked bar (series = 2nd dim, capped)
- 1 categorical + >=2 numeric but wide -> table when >6 numeric columns
- anything else                   -> table
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

_TEMPORAL_NAME_RE = re.compile(
    r"(date|day|week|month|quarter|year|time|period|日期|月|年|日)", re.I
)
#: Cap for bar chart categories — a 500-row result is not a chart.
_MAX_CATEGORIES = 20
_MAX_NUMERIC_SERIES = 4
_MAX_SCAN_ROWS = 200


def _is_numberish(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s or any(ch.isalpha() for ch in s):
            return False
        try:
            float(s)
            return True
        except ValueError:
            return False
    return False


def _is_temporal(v: Any) -> bool:
    if isinstance(v, (datetime, date)):
        return True
    if isinstance(v, str):
        s = v.strip()
        if len(s) < 6 or len(s) > 32:
            return False
        for fmt in ("%Y-%m-%d", "%Y-%m", "%Y/%m", "%Y-%m-%d %H:%M:%S", "%Y年%m月"):
            try:
                datetime.strptime(s, fmt)
                return True
            except ValueError:
                continue
    return False


def _classify_columns(rows: list[dict], columns: list[str]) -> dict[str, str]:
    """column -> 'temporal' | 'numeric' | 'categorical', by majority vote over
    a bounded sample of rows."""
    kinds: dict[str, list[str]] = {c: [] for c in columns}
    sample = rows[:_MAX_SCAN_ROWS]
    for row in sample:
        for c in columns:
            v = row.get(c)
            if v is None or (isinstance(v, str) and not v.strip()):
                continue  # nulls abstain
            if _is_numberish(v):
                kinds[c].append("numeric")
            elif _is_temporal(v):
                kinds[c].append("temporal")
            else:
                kinds[c].append("categorical")
    out: dict[str, str] = {}
    for c, votes in kinds.items():
        if not votes:
            out[c] = "categorical"
        else:
            out[c] = max(set(votes), key=votes.count)
    return out


def suggest_chart(
    rows: list[Any], columns: list[str] | None = None
) -> dict[str, Any] | None:
    """Deterministic visualization hint for a tabular result. ``None`` when
    the result does not warrant a chart (or anything went wrong)."""
    try:
        if not rows or not isinstance(rows, list) or not isinstance(rows[0], dict):
            return None
        cols = list(columns) if columns else list(rows[0].keys())
        cols = [c for c in cols if c][:12]
        if not cols:
            return None
        kinds = _classify_columns(rows, cols)
        temporals = [c for c in cols if kinds[c] == "temporal"]
        numerics = [c for c in cols if kinds[c] == "numeric"]
        categoricals = [c for c in cols if kinds[c] == "categorical"]

        base: dict[str, Any] = {
            "columns": {
                c: kinds[c] for c in cols
            },
            "row_count": len(rows),
        }

        # 1 temporal + numeric(s): the canonical trend/line shape.
        if len(temporals) == 1 and 1 <= len(numerics) <= _MAX_NUMERIC_SERIES:
            return {
                **base,
                "chart_type": "line",
                "x": temporals[0],
                "y": numerics[:_MAX_NUMERIC_SERIES],
                "reason": "one time column with measures — trend over time",
            }

        # 2 categoricals + 1 numeric: dimension + series breakdown.
        if len(categoricals) >= 2 and len(numerics) >= 1:
            distinct_series = len({r.get(categoricals[1]) for r in rows[:_MAX_SCAN_ROWS]})
            if distinct_series <= _MAX_CATEGORIES:
                return {
                    **base,
                    "chart_type": "stacked_bar",
                    "x": categoricals[0],
                    "series": categoricals[1],
                    "y": numerics[:1],
                    "reason": (
                        f"two dimensions with {distinct_series} series values — "
                        "stacked comparison"
                    ),
                }

        # 1 categorical + numeric(s): ranked bar.
        if len(categoricals) >= 1 and 1 <= len(numerics) <= _MAX_NUMERIC_SERIES:
            chart_type = "bar"
            payload = {
                **base,
                "chart_type": chart_type,
                "x": categoricals[0],
                "y": numerics[:_MAX_NUMERIC_SERIES],
                "max_categories": _MAX_CATEGORIES,
                "reason": "categories with measures — ranked comparison",
            }
            if len(numerics) > 6:
                return {**base, "chart_type": "table",
                        "reason": "too many measures for a readable chart"}
            return payload

        # Temporal but no numeric: a timeline of labels, not a chart.
        return {**base, "chart_type": "table", "reason": "no numeric measure to plot"}
    except Exception as e:  # a suggestion must never break an answer
        logger.debug("chart_suggestor: suggestion failed: %s", e)
        return None
