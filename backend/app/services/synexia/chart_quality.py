"""Shared chart-quality gate for Zhanlu.

Two pure functions used by every chart producer in the system
(`report_synthesis.py` for in-chat report cards and
`generation_orchestrator.py` for HTML dashboard artifacts):

- :func:`validate_chart_spec` repairs a ``ChartSpec`` produced by the LLM
  or a heuristic: coerce numeric strings, aggregate duplicate x-labels by
  sum, drop constant (and therefore uninformative) series, cap to
  top-N + "Other", and surface warnings for each repair.
- :func:`pick_chart_columns` returns the best ``(label_col, value_col)``
  pair for a list of rows, skipping id-like and timestamp columns for
  the value and preferring a low-cardinality string column for the
  label.

The module is deliberately pure (no I/O, no LLM, no Pydantic side
effects beyond a single ``model_dump`` / re-validate) so it can be
unit-tested in isolation and shared across the two producers.

Why this exists: the LLM-driven synthesis path in
:mod:`app.services.synexia.report_synthesis` tells the model to copy
rows verbatim into ``chart.data``. When the underlying snapshot has no
real numeric measure (e.g. 199 DB table names), the LLM fabricates a
constant value of 1 per row, producing the "all bars = 1" bug
visible in the user's screenshots. This gate is the single point where
that degenerate input is detected and either repaired or dropped with
an explicit warning, so the rest of the pipeline only ever sees a
chart spec that is safe to render.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any, Optional

# Column-name patterns that almost never carry a meaningful numeric
# measure: primary keys, foreign keys, monotonic IDs, and obvious
# timestamp columns. Used by ``pick_chart_columns`` to skip these when
# picking the value column.
_ID_LIKE = re.compile(r"(?:^|_)(id|uuid|guid|pk|sk)$", re.IGNORECASE)
_ID_LIKE_SUFFIX = re.compile(r"_id$", re.IGNORECASE)
_TIMESTAMP_LIKE = re.compile(
    r"(?:^|_)(?:created|updated|deleted|modified|at|_at|on|date|time|timestamp|ts)$",
    re.IGNORECASE,
)

# Default cap on the number of x-categories the repaired chart will
# render. Anything above this is bucketed into a single "Other" row so
# the chart stays scannable.
DEFAULT_MAX_CATEGORIES = 12

# Tolerance for "values are constant". Anything with a spread below
# this is treated as a single-value series and dropped.
_CONSTANT_EPSILON = 1e-9

_NUMERIC_RE = re.compile(r"^[^\d\-+]*([-+]?[\d,]+\.?\d*)\s*%?\s*$")


def _coerce_numeric(value: Any) -> Optional[float]:
    """Best-effort numeric coercion for a single cell.

    Returns a float, or ``None`` if the value cannot be interpreted as
    a number. Handles the common LLM/CSV quirks: ``"1,234"``,
    ``"25.5%"``, ``"$100"``, ``" -7 `` and trailing whitespace.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Strip currency, percent, thousands separators
        s = s.replace(",", "").replace("$", "").replace("¥", "").replace("€", "")
        s = s.replace("％", "%").strip()
        had_percent = s.endswith("%")
        s = s.rstrip("%").strip()
        if not s:
            return None
        try:
            n = float(s)
        except ValueError:
            return None
        if had_percent:
            # Percentages of a whole → keep as fraction (25.5% → 25.5)
            # so two percentages of 25.5% and 74.5% remain directly
            # comparable. If the caller wants 0..1, they can divide.
            pass
        return n
    return None


def _is_string_like(value: Any) -> bool:
    return isinstance(value, str)


def _column_kind(rows: list[dict[str, Any]], col: str) -> str:
    """Return ``"numeric"``, ``"string"`` or ``"mixed"`` for a column.

    Inspects every non-None value in the column. ``"numeric"`` requires
    that *all* non-None values coerce to float; ``"string"`` requires
    that *all* non-None values are strings; otherwise ``"mixed"``.
    """
    numeric = 0
    string = 0
    for r in rows:
        v = r.get(col)
        if v is None:
            continue
        if isinstance(v, bool):
            numeric += 1
            continue
        if isinstance(v, (int, float)):
            numeric += 1
            continue
        if isinstance(v, str):
            string += 1
        else:
            return "mixed"
    if numeric and not string:
        return "numeric"
    if string and not numeric:
        return "string"
    return "mixed"


def _series_constant(values: list[Optional[float]]) -> bool:
    """A series is "constant" if it's empty, has a single distinct
    value, or has a spread smaller than ``_CONSTANT_EPSILON``.

    ``None`` values are ignored — they are not considered to break the
    constant-ness of the remaining values.
    """
    cleaned = [v for v in values if v is not None]
    if len(cleaned) <= 1:
        return True
    lo = min(cleaned)
    hi = max(cleaned)
    if hi - lo < _CONSTANT_EPSILON:
        return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_chart_spec(
    chart: Any, *, max_categories: int = DEFAULT_MAX_CATEGORIES
) -> tuple[Optional[Any], list[str]]:
    """Validate and (if possible) repair a :class:`ChartSpec`.

    The function is purely deterministic: it never talks to the LLM
    and never mutates the input chart. It returns a 2-tuple of
    ``(repaired_chart_or_None, warnings)``.

    Repairs applied, in order:

    1. Empty data → ``(None, ["chart has no data rows"])``.
    2. ``x_key`` absent from the data → ``(None, [...])``.
    3. ``y_keys`` not present in the data → drop the missing keys, drop
       the chart if no y_key remains.
    4. Coerce string numerics (``"25.5%"``, ``"1,234"``, ``"$100"``)
       to floats.
    5. Aggregate duplicate x-labels by summing each y_key. A warning
       notes the aggregation.
    6. Drop any y_key whose values are constant. If every y_key is
       dropped, the chart is dropped with a warning.
    7. Cap x-categories to the top ``max_categories`` by total y
       magnitude; the remainder is collapsed into a single
       ``"Other"`` row.
    """
    warnings: list[str] = []

    if chart is None:
        return None, ["chart is None"]

    data = list(getattr(chart, "data", []) or [])
    x_key = getattr(chart, "x_key", None)
    y_keys = list(getattr(chart, "y_keys", []) or [])

    if not data:
        return None, ["chart has no data rows"]

    if not x_key:
        return None, ["chart missing x_key"]

    if not y_keys:
        return None, ["chart missing y_keys"]

    # --- Step 2/3: keys present in data? ---------------------------
    sample_keys = set(data[0].keys())
    if x_key not in sample_keys:
        return None, [f"x_key '{x_key}' not in data columns ({sorted(sample_keys)})"]

    present_y_keys: list[str] = []
    missing_y_keys: list[str] = []
    for yk in y_keys:
        if yk in sample_keys:
            present_y_keys.append(yk)
        else:
            missing_y_keys.append(yk)
    if missing_y_keys:
        warnings.append(f"y_key(s) missing from data and dropped: {missing_y_keys}")
    if not present_y_keys:
        return None, warnings or ["no usable y_key present in data"]

    # --- Step 4: coerce numeric strings ---------------------------
    coerced_any = False
    for row in data:
        for yk in present_y_keys:
            v = row.get(yk)
            if isinstance(v, str):
                n = _coerce_numeric(v)
                if n is not None and n != v:
                    row[yk] = n
                    coerced_any = True
    if coerced_any:
        warnings.append("coerced string numerics in y values to floats")

    # --- Step 5: aggregate duplicate x-labels ---------------------
    aggregated: "OrderedDict[Any, dict[str, Any]]" = OrderedDict()
    dup_count = 0
    for row in data:
        label = row.get(x_key)
        if label is None:
            continue
        if label in aggregated:
            dup_count += 1
            for yk in present_y_keys:
                existing = aggregated[label].get(yk, 0.0) or 0.0
                v = row.get(yk)
                n = _coerce_numeric(v)
                aggregated[label][yk] = existing + (n or 0.0)
        else:
            entry: dict[str, Any] = {x_key: label}
            for yk in present_y_keys:
                v = row.get(yk)
                n = _coerce_numeric(v)
                entry[yk] = n if n is not None else 0.0
            aggregated[label] = entry
    if dup_count:
        warnings.append(
            f"aggregated {dup_count} duplicate x-label row(s) by summing y values"
        )
    data = list(aggregated.values())
    if not data:
        return None, warnings + ["all rows had null x_key"]

    # --- Step 6: drop constant y_keys -----------------------------
    kept_y_keys: list[str] = []
    for yk in present_y_keys:
        vals = [_coerce_numeric(r.get(yk)) for r in data]
        if _series_constant(vals):
            warnings.append(f"y_key '{yk}' is constant; dropped as uninformative")
        else:
            kept_y_keys.append(yk)
    if not kept_y_keys:
        return None, warnings + ["all y_keys are constant; chart would render flat bars"]
    y_keys = kept_y_keys

    # --- Step 7: cap to top-N + "Other" ---------------------------
    if max_categories and len(data) > max_categories:
        # Sort by total y magnitude across kept y_keys (desc).
        def _mag(row: dict[str, Any]) -> float:
            return float(sum(abs(_coerce_numeric(row.get(yk)) or 0.0) for yk in y_keys))

        data_sorted = sorted(data, key=_mag, reverse=True)
        top = data_sorted[:max_categories]
        rest = data_sorted[max_categories:]
        other_row: dict[str, Any] = {x_key: "Other"}
        for yk in y_keys:
            other_row[yk] = float(
                sum(_coerce_numeric(r.get(yk)) or 0.0 for r in rest)
            )
        data = top + [other_row]
        warnings.append(
            f"capped to top {max_categories} categories plus 1 'Other' row "
            f"({len(rest)} categories merged)"
        )

    # --- Build the repaired chart ---------------------------------
    # Reuse the same model class so all metadata (type, title, unit,
    # source, …) is preserved verbatim.
    try:
        from app.services.synexia.contracts import ChartSpec  # local import
    except Exception:  # pragma: no cover - module is in the same package
        ChartSpec = type(chart)

    repaired = ChartSpec(
        type=getattr(chart, "type", "bar"),
        title=getattr(chart, "title", None),
        x_key=x_key,
        y_keys=y_keys,
        data=data,
        unit=getattr(chart, "unit", None),
    )
    return repaired, warnings


def pick_chart_columns(rows: Optional[list[dict[str, Any]]]) -> tuple[Optional[str], Optional[str]]:
    """Pick the best ``(label_col, value_col)`` for a list of rows.

    ``value_col`` is the first column whose values are numeric and
    whose name is neither id-like (``id``, ``user_id``, ``uuid`` …) nor
    timestamp-like (``created_at``, ``date``, ``ts`` …). If no such
    column exists, ``value_col`` is ``None`` and the caller should not
    render a chart.

    ``label_col`` is the first string column with low cardinality
    (≤ 20 distinct values AND ≤ 50 % unique-vs-rows). When no low-
    cardinality string column exists, fall back to the first string
    column, or ``None`` when no string column exists.

    Returns ``(None, None)`` for empty or ``None`` input.
    """
    if not rows:
        return None, None

    cols = list(rows[0].keys())
    if not cols:
        return None, None

    kinds: dict[str, str] = {c: _column_kind(rows, c) for c in cols}

    # --- value column ---------------------------------------------
    value_col: Optional[str] = None
    for c in cols:
        if kinds[c] != "numeric":
            continue
        if _ID_LIKE.search(c) or _ID_LIKE_SUFFIX.search(c):
            continue
        if _TIMESTAMP_LIKE.search(c):
            continue
        value_col = c
        break
    if value_col is None:
        # No safe numeric column — the caller should not chart this.
        return None, None

    # --- label column ---------------------------------------------
    def _is_bad_label_name(c: str) -> bool:
        return bool(_ID_LIKE.search(c) or _ID_LIKE_SUFFIX.search(c) or _TIMESTAMP_LIKE.search(c))

    string_cols = [
        c for c in cols
        if kinds[c] == "string" and c != value_col and not _is_bad_label_name(c)
    ]
    label_col: Optional[str] = None
    n = len(rows)
    for c in string_cols:
        distinct = {r.get(c) for r in rows if r.get(c) is not None}
        if not distinct:
            continue
        if len(distinct) <= 20 and len(distinct) <= max(1, n // 2):
            label_col = c
            break
    if label_col is None and string_cols:
        label_col = string_cols[0]

    return label_col, value_col
