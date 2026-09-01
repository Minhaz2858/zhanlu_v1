"""Data profiling helpers for the deck planner.

Pure functions with no LLM dependency.  Given a list of row dicts (or a
list of lists + headers), detect column types, cardinality, and produce
chart suggestions for the structured renderer.

These are deliberately standalone so they can be reused by the audit /
repair layer and future features (see the design doc in
``docs/superpowers/specs/``).
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Column type detection
# ---------------------------------------------------------------------------

_NUMERIC = (int, float)
_BOOL = (bool,)


def _is_numeric(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, _NUMERIC):
        return True
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return False
        try:
            float(s)
            return True
        except ValueError:
            return False
    return False


def _to_float(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, _NUMERIC):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _is_temporal(v: Any) -> bool:
    """Loose temporal detection — checks for a date-like prefix.

    Deliberately cheap: full date parsing is unnecessary for chart-type
    suggestion.  Matches ``YYYY-MM``, ``YYYY-MM-DD``, ``YYYY/MM/DD`` and
    ISO timestamps.
    """
    if not isinstance(v, str):
        return False
    s = v.strip()
    n = len(s)
    if n < 7:
        return False
    if not s[:4].isdigit():
        return False
    # YYYY-MM / YYYY/MM (exactly 7 chars).
    if n == 7:
        return s[4] in ("-", "/")
    # YYYY-MM-DD / YYYY/MM/DD and ISO timestamps (YYYY-MM-DD[T ]...).
    if n >= 10 and s[4] in ("-", "/") and s[7] in ("-", "/"):
        return True
    return False


def detect_column_type(values: Iterable[Any]) -> str:
    """Classify a column as numeric | temporal | categorical | empty.

    Classification is majority-based over the non-null values, so a
    single stray string in a numeric column doesn't flip the result.
    """
    vals = [v for v in values if v is not None and v != ""]
    if not vals:
        return "empty"

    numeric = sum(1 for v in vals if _is_numeric(v))
    temporal = sum(1 for v in vals if _is_temporal(v))
    n = len(vals)

    if numeric / n >= 0.7:
        return "numeric"
    if temporal / n >= 0.7:
        return "temporal"
    return "categorical"


def cardinality(values: Iterable[Any]) -> int:
    """Number of distinct values in a column (ignoring null/empty)."""
    seen = set()
    for v in values:
        if v is None or v == "":
            continue
        seen.add(v)
    return len(seen)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def profile_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Profile a list of row dicts.

    Returns ``{column_name: {type, cardinality, sample}}``.  A sample of
    up to 5 values is included for the planner's context window (samples
    are for LLM prompts only — never embedded into the rendered doc text).
    """
    if not rows:
        return {}

    columns: dict[str, list[Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, val in row.items():
            columns.setdefault(key, []).append(val)

    profile: dict[str, dict[str, Any]] = {}
    for col, vals in columns.items():
        ctype = detect_column_type(vals)
        card = cardinality(vals)
        # Unique non-null sample values, capped at 5.
        sample: list[Any] = []
        seen = set()
        for v in vals:
            if v is None or v == "" or v in seen:
                continue
            seen.add(v)
            sample.append(v)
            if len(sample) >= 5:
                break
        profile[col] = {"type": ctype, "cardinality": card, "sample": sample}

    return profile


def suggest_charts(profile: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Suggest chart specs from a column profile.

    Returns a list of ``{chart_type, x_key, y_keys, title}`` dicts.  The
    heuristic is intentionally simple and deterministic:

    * a temporal column + one-or-more numeric columns → ``line``
    * a categorical column (cardinality <= 12) + numeric → ``bar``
    * a categorical column (cardinality <= 8) + a single numeric → ``pie``

    At most 3 suggestions are returned, ordered by signal strength.
    """
    columns = list(profile.keys())
    numeric_cols = [c for c in columns if profile[c]["type"] == "numeric"]
    temporal_cols = [c for c in columns if profile[c]["type"] == "temporal"]
    categorical_cols = [
        c
        for c in columns
        if profile[c]["type"] == "categorical" and profile[c]["cardinality"] <= 12
    ]

    suggestions: list[dict[str, Any]] = []

    # Temporal x-axis → line chart is the strongest signal.
    for tcol in temporal_cols:
        if not numeric_cols:
            continue
        suggestions.append(
            {
                "chart_type": "line",
                "x_key": tcol,
                "y_keys": numeric_cols[:3],
                "title": f"{numeric_cols[0]} over {tcol}",
            }
        )
        if len(suggestions) >= 3:
            break

    # Categorical breakdown → bar chart.
    if len(suggestions) < 3:
        for ccol in categorical_cols:
            if not numeric_cols:
                continue
            suggestions.append(
                {
                    "chart_type": "bar",
                    "x_key": ccol,
                    "y_keys": numeric_cols[:3],
                    "title": f"{numeric_cols[0]} by {ccol}",
                }
            )
            if len(suggestions) >= 3:
                break

    # Low-cardinality share-of-whole → pie.
    if len(suggestions) < 3:
        for ccol in categorical_cols:
            if profile[ccol]["cardinality"] > 8 or not numeric_cols:
                continue
            suggestions.append(
                {
                    "chart_type": "pie",
                    "x_key": ccol,
                    "y_keys": numeric_cols[:1],
                    "title": f"Share of {numeric_cols[0]} by {ccol}",
                }
            )
            if len(suggestions) >= 3:
                break

    return suggestions


def summarize_profile(profile: dict[str, dict[str, Any]], max_columns: int = 25) -> str:
    """Render a compact, LLM-friendly summary of a profile.

    Used inside the planner prompt.  Capped at ``max_columns`` so a very
    wide table doesn't blow the context window.
    """
    if not profile:
        return "(no data)"

    lines: list[str] = []
    for col, meta in list(profile.items())[:max_columns]:
        sample = ", ".join(str(s) for s in meta.get("sample", [])[:3])
        lines.append(
            f"- {col}: {meta['type']} (cardinality={meta['cardinality']})"
            + (f" e.g. {sample}" if sample else "")
        )

    if len(profile) > max_columns:
        lines.append(f"... ({len(profile) - max_columns} more columns omitted)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Group-by breakdowns
# ---------------------------------------------------------------------------


def group_by_profile(
    rows: list[dict[str, Any]],
    dimension: str,
    value_col: str,
    top_n: int | None = 10,
) -> list[dict[str, Any]]:
    """Compute a top-N group-by breakdown for one dimension.

    Given rows and a categorical ``dimension`` plus a numeric ``value_col``,
    aggregates ``value_col`` per group and returns the top-``top_n`` groups
    sorted by aggregated value descending, each as::

        {"group": <label>, "value": <float>, "share_pct": <float 0-100>}

    ``share_pct`` is the group's share of the dimension total.  Groups with
    null/empty labels are skipped.  Deterministic ordering — ties break by
    group label ascending.  Pass ``top_n=None`` to return ALL groups.
    """
    groups: dict[str, float] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = r.get(dimension)
        val = _to_float(r.get(value_col))
        if key is None or key == "" or val is None:
            continue
        groups[str(key)] = groups.get(str(key), 0.0) + val

    if not groups:
        return []

    total = sum(groups.values())
    sorted_groups = sorted(groups.items(), key=lambda kv: (-kv[1], str(kv[0])))
    if top_n is not None:
        sorted_groups = sorted_groups[:top_n]
    return [
        {"group": g, "value": v, "share_pct": v / total * 100 if total else 0.0}
        for g, v in sorted_groups
    ]
