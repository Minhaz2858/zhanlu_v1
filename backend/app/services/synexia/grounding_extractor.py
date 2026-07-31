"""Deterministic grounding extractor for the FINALIZE response prompt.

Distills each observation into a compact, token-bounded block so the
response generator writes grounded, specific responses ("Q2 revenue was
189M CNY, top material at 35% share") instead of generic ones ("I've
processed your request").

Pure & deterministic — no LLM calls.  All field reads are defensive
(observation objects may be partial in tests or detached executions).

Char-budget policy (spec §3.1): the assembled block is capped to
``SYNEXIA_GROUNDING_MAX_CHARS`` (default 2000).  Under the cap, **data
lines are dropped before metadata lines** — columns/counts/aggregate
stats are higher-signal-per-char than raw rows, so rows truncate first.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

# Per-contribution caps applied before the global cap.
_SQL_MAX_CHARS = 400
_TOOL_RESULT_MAX_CHARS = 500
_TOP_ROWS = 3
_ROW_LINE_MAX_CHARS = 200


def extract_grounding(
    observations: Iterable[Any],
    *,
    max_chars: int | None = None,
) -> str:
    """Build a compact findings block from observations.

    Args:
        observations: iterable of ObservationRecord-like objects (may be
            partial; all fields read defensively).
        max_chars: overall char cap. Defaults to
            ``settings.SYNEXIA_GROUNDING_MAX_CHARS`` (2000).

    Returns:
        A multi-line string with one ``=== #<seq> <tool> (ok|FAILED) ===``
        block per observation, or ``"No actions taken."`` when empty.
        Under the cap, data lines are dropped from the bottom before any
        metadata line is removed.
    """
    if max_chars is None:
        from app.config import settings
        max_chars = getattr(settings, "SYNEXIA_GROUNDING_MAX_CHARS", 2000)

    if not observations:
        return "No actions taken."

    parts = [_extract_one(obs) for obs in observations]

    # Metadata lines first (one batch across all obs), then data lines —
    # so a cap shortfall drops data (rows) before metadata.
    meta_lines: list[str] = []
    data_lines: list[str] = []
    for p in parts:
        meta_lines.append(p["header"])
        for m in p["meta"]:
            meta_lines.append("  " + m)
    for p in parts:
        for d in p["data"]:
            data_lines.append("  " + d)

    # Drop data lines from the bottom until we fit (metadata preserved).
    while data_lines and len("\n".join(meta_lines + data_lines)) > max_chars:
        data_lines.pop()

    body = "\n".join(meta_lines + data_lines)
    if not body.strip():
        return "No actions taken."
    return body[:max_chars]


# ── Per-obs-type extractors ───────────────────────────────────────────────

def _extract_one(obs: Any) -> dict:
    """Return ``{'header', 'meta', 'data'}`` for one observation."""
    otype = (getattr(obs, "observation_type", "") or "").lower()
    tool = getattr(obs, "tool_name", None) or otype or "action"
    seq = getattr(obs, "seq", "?")
    success = getattr(obs, "success", False)
    header = f"=== #{seq} {tool} ({'ok' if success else 'FAILED'}) ==="

    if not success:
        err = getattr(obs, "error_message", None) or "failed"
        return {"header": header, "meta": [f"error: {err}"], "data": []}

    dispatch = {
        "nl2sql": _extract_nl2sql,
        "synthesize": _extract_synthesize,
        "sandbox": _extract_sandbox,
    }
    fn = dispatch.get(otype, _extract_tool)
    meta, data = fn(obs)
    return {"header": header, "meta": meta, "data": data}


def _result_data_dict(obs: Any) -> dict:
    rd = getattr(obs, "result_data", None)
    return rd if isinstance(rd, dict) else {}


def _extract_nl2sql(obs: Any) -> tuple[list[str], list[str]]:
    rd = _result_data_dict(obs)
    sql = str(rd.get("sql") or "")[:_SQL_MAX_CHARS]
    rows = rd.get("data") or []
    if not isinstance(rows, list):
        rows = []
    cols = rd.get("columns")
    if not cols and rows and isinstance(rows[0], dict):
        cols = list(rows[0].keys())
    cols = cols or []

    meta: list[str] = []
    if sql:
        meta.append(f"sql: {sql}")
    meta.append(f"rows: {len(rows)}")
    if cols:
        meta.append(f"columns: {', '.join(map(str, cols))}")
    for col in cols:
        vals = _numeric_values(rows, col)
        if vals:
            meta.append(
                f"{col}: sum={sum(vals):.4g} min={min(vals):.4g} max={max(vals):.4g}"
            )

    data: list[str] = []
    for row in rows[:_TOP_ROWS]:
        if isinstance(row, dict):
            data.append(json.dumps(row, ensure_ascii=False, default=str)[:_ROW_LINE_MAX_CHARS])
    return meta, data


def _extract_synthesize(obs: Any) -> tuple[list[str], list[str]]:
    rd = _result_data_dict(obs)
    meta: list[str] = []
    if rd.get("title"):
        meta.append(f"title: {rd['title']}")
    for kpi in rd.get("kpis") or []:
        if isinstance(kpi, dict):
            meta.append(f"kpi {kpi.get('label', '?')}: {kpi.get('value', '?')}")
    chart = rd.get("chart")
    if isinstance(chart, dict) and chart.get("title"):
        meta.append(f"chart: {chart['title']}")
    data = [
        f"insight: {i.get('text', '')}"
        for i in (rd.get("insights") or [])
        if isinstance(i, dict)
    ]
    return meta, data


def _extract_sandbox(obs: Any) -> tuple[list[str], list[str]]:
    rd = _result_data_dict(obs)
    meta: list[str] = []
    atype = rd.get("artifact_type") or rd.get("format")
    if atype:
        meta.append(f"artifact: {atype}")
    if rd.get("title"):
        meta.append(f"title: {rd['title']}")
    ids = getattr(obs, "artifact_ids", None) or []
    if ids:
        meta.append(f"artifact_ids: {', '.join(map(str, ids))}")
    return meta, []


def _extract_tool(obs: Any) -> tuple[list[str], list[str]]:
    text = getattr(obs, "result_text", None)
    if not text:
        rd = _result_data_dict(obs)
        text = rd.get("result") or rd.get("summary") or ""
    text = str(text)[:_TOOL_RESULT_MAX_CHARS]
    return [], [f"result: {text}"] if text else []


def _numeric_values(rows: list, col: str) -> list[float]:
    out: list[float] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        v = r.get(col)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.append(v)
    return out
