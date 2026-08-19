"""Server-driven generation orchestrator.

Guarantees that a user request for a downloadable file (docx / pptx / pdf /
html) results in an actual artifact — never a bare "I'll create …" reply.

Two responsibilities:

1. **Marker fulfillment (async fix).** The chat runtime detects
   ``◤MD_DOCX◤`` / ``◤HTML_DOCX◤`` / ``◤PPTX◤`` markers in the assistant
   reply and routes them into :func:`_create_artifact_tool`. That handler is
   ``async``; the legacy call sites invoked it *without* ``await``, producing
   a never-awaited coroutine — so no artifact was ever created. This module
   awaits it properly.

2. **Doc-request fallback.** When the turn-action router determined the user
   asked for a file (``doc_format`` is set) but the LLM produced *neither* a
   marker *nor* a successful ``create_artifact`` tool call, the orchestrator
   synthesizes a minimal ReportCard payload from the assistant's prose and
   creates the artifact server-side. The user always receives a downloadable
   file or a clear, logged error — never silence.

This is the Q1 (server-driven orchestrator) decision from the end-to-end fix.
It is best-effort and non-fatal: any failure is logged and swallowed so the
chat response itself is never broken.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.artifact import Artifact
from app.services.artifact_markers import find_markers
from app.services.synexia.intent_router import FileFormat

logger = logging.getLogger(__name__)

# Cap embedded data so a runaway query never produces a 100 MB HTML artifact.
_DASHBOARD_MAX_ROWS = 500
_DASHBOARD_MAX_HTML_BYTES = 2 * 1024 * 1024  # 2 MB cap on file read
_DASHBOARD_TITLE_MAX = 120

def _create_artifact_tool(args, db=None, context=None):  # noqa: ANN001
    """Lazy resolve the real async handler.

    Kept as a module-level indirection so tests can ``patch.object`` the
    symbol without importing the heavy exporter stack at module load time.
    The real handler is async; this wrapper simply returns its coroutine so
    callers ``await`` it as usual.
    """
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool as _impl

    return _impl(args=args, db=db, context=context)


# Marker kinds we know how to route into _create_artifact_tool.
_MARKER_KIND_TO_TYPE = {
    "MD_DOCX": "docx",
    "HTML_DOCX": "docx",
    "PPTX": "pptx",
    "DASHBOARD": "html",
}

# FileFormat values that map to a create_artifact ``type``. Formats that the
# exporter pipeline cannot produce (xlsx, md) are intentionally absent — we do
# not fabricate artifacts we cannot render. ``dashboard`` is the one synthetic
# format that IS renderable: it produces a self-contained interactive HTML
# dashboard artifact (Chart.js + KPI cards + sortable table).
_FORMAT_TO_ARTIFACT_TYPE = {
    "docx": "docx",
    "pptx": "pptx",
    "pdf": "pdf",
    "html": "html",
    "dashboard": "html",
}


def _artifact_matches_requested_format(artifact_id: str, doc_format: str, db) -> bool:  # noqa: ANN001
    """True only when the stored Artifact's type/payload signal matches the
    explicitly requested doc_format (pptx/docx/xlsx/dashboard). Report cards
    and other html/text artifacts never satisfy an explicit file request."""
    expected = _FORMAT_TO_ARTIFACT_TYPE.get(doc_format)
    if expected is None:
        # xlsx / md are not renderable by this pipeline; no artifact can be
        # considered a match, so the fallback keeps trying.
        return False
    try:
        artifact = db.get(Artifact, artifact_id)
    except Exception:  # noqa: BLE001 — db is best-effort here
        artifact = None
    if artifact is None:
        return False
    atype = (artifact.artifact_type or "").lower().strip()
    # dashboard requests are satisfied by html artifacts (the orchestrator
    # fallback itself produces type=html), never by html_report report cards.
    return atype == expected


def _artifact_satisfies_deliverable(artifact_id: str, doc_format: str, db) -> bool:  # noqa: ANN001
    """Goal-Contract content gate: True only when the stored Artifact both
    matches the requested format AND carries a non-empty, non-failed payload
    (a build actually produced it).

    A report card (html_report) never satisfies an explicit doc deliverable,
    and a ``failed`` artifact or one with no built version (``current_version_id``
    is None) is treated as "not delivered" so the fallback still produces the
    user's deliverable instead of a dead file. Flag-gated by
    ``GOAL_CONTRACT_ENABLED`` at the call site.
    """
    if not _artifact_matches_requested_format(artifact_id, doc_format, db):
        return False
    try:
        artifact = db.get(Artifact, artifact_id)
    except Exception:  # noqa: BLE001 — db is best-effort here
        return False
    if artifact is None:
        return False
    if (artifact.status or "").lower() == "failed":
        return False
    # No current version ⇒ nothing was actually built (empty deliverable).
    return bool(artifact.current_version_id)


def _marker_to_artifact_args(kind: str, payload: dict, filename: str) -> Optional[dict]:
    """Translate a parsed marker into ``_create_artifact_tool`` args.

    Returns ``None`` for unknown kinds. Note: the current
    ``_payload_to_reportcard`` inside the handler reads ReportCard-shaped
    keys, so we forward the source path *and* a summary hint; the handler's
    ReportCard mapping is the single place that converts the payload.
    """
    artifact_type = _MARKER_KIND_TO_TYPE.get(kind)
    if artifact_type is None:
        return None
    # Forward the file path the skill wrote (outputs/...) so a future
    # path-aware renderer can pick it up, plus a ReportCard title/summary so
    # the current renderer produces a non-empty skeleton.
    fwd_payload = dict(payload)
    fwd_payload.setdefault("title", filename or f"{artifact_type}-artifact")
    # DASHBOARD markers carry an html_path; consume it here so the html
    # artifact handler receives html_content directly (the marker-path
    # key was previously forwarded but not consumed by any renderer).
    if kind == "DASHBOARD":
        html_content = _read_dashboard_html(payload.get("html_path"))
        if html_content is None:
            logger.warning(
                "orchestrator: DASHBOARD marker has no readable html_path=%r; "
                "falling back to no-content artifact",
                payload.get("html_path"),
            )
        else:
            fwd_payload["html_content"] = html_content
        title = str(fwd_payload.pop("title", None) or filename or "Dashboard")
    else:
        title = filename or f"{artifact_type}-artifact"
    return {
        "type": artifact_type,
        "title": title,
        "payload": fwd_payload,
        "skill": kind.lower(),
    }


def _read_dashboard_html(html_path: Any) -> Optional[str]:
    """Safely read a dashboard HTML file produced by the skill body.

    Returns ``None`` if the path is missing, the file does not exist, is
    larger than ``_DASHBOARD_MAX_HTML_BYTES``, or fails to read. Caps the
    read so a runaway / hostile file cannot produce a 100 MB artifact.
    """
    if not html_path or not isinstance(html_path, str):
        return None
    try:
        path = Path(html_path)
        if not path.is_absolute():
            # Marker paths are relative to the agent working dir (backend/).
            cwd = Path.cwd()
            # Prefer cwd, but fall back to the file's literal path which is
            # what the LLM actually wrote under outputs/.
            candidates = [cwd / path, Path(__file__).resolve().parents[2] / path]
            resolved = next((p for p in candidates if p.exists() and p.is_file()), None)
            if resolved is None:
                return None
            path = resolved
        if not path.exists() or not path.is_file():
            return None
        size = path.stat().st_size
        if size <= 0 or size > _DASHBOARD_MAX_HTML_BYTES:
            logger.warning(
                "orchestrator: dashboard html %s size=%d outside (0, %d]; skipping",
                path, size, _DASHBOARD_MAX_HTML_BYTES,
            )
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("orchestrator: failed to read dashboard html %r: %s", html_path, exc)
        return None


async def fulfill_markers(
    assistant_content: str,
    db: Session,
    context: dict,
) -> tuple[str, list[dict]]:
    """Detect markers in the reply, create an artifact for each, strip markers.

    Args:
        assistant_content: The raw assistant text (may contain markers).
        db:                Active DB session.
        context:           ``{conversation_id, agent_app_id}`` for the tool.

    Returns:
        ``(cleaned_content, created_artifacts)`` where ``created_artifacts``
        is a list of successful ``_create_artifact_tool`` result dicts.
    """
    created: list[dict] = []
    try:
        markers = find_markers(assistant_content)
    except Exception as exc:  # find_markers is defensive, but never break chat
        logger.warning("orchestrator: marker scan failed (non-fatal): %s", exc)
        return assistant_content, created

    for m in markers:
        args = _marker_to_artifact_args(m.kind, m.payload, m.filename)
        if args is None:
            continue
        try:
            result = await _create_artifact_tool(args=args, db=db, context=context)
            if isinstance(result, dict) and result.get("success"):
                created.append(result)
            else:
                logger.warning(
                    "orchestrator: marker %s did not produce an artifact: %s",
                    m.kind, (result or {}).get("error") if isinstance(result, dict) else result,
                )
        except Exception as exc:
            logger.warning("orchestrator: marker %s execution failed (non-fatal): %s", m.kind, exc)

    from app.services.artifact_markers import strip_markers
    cleaned = strip_markers(assistant_content)
    return cleaned, created


# Procedural narrator chatter the LLM emits while working ("I'm going to
# build …", "Let me load the PPTX skill …"). These sentences describe the
# assistant's own process — they must never become a report title or summary.
_CHATTER_RE = re.compile(
    r"^(?:"
    r"i['’]m going to|i am going to|i['’]ll|i will|i need to|i must|"
    r"let me|let['’]s|first[,\s]|next[,\s]i|now i|i['’]m now|"
    r"i have (?:now )?(?:loaded|created|generated|built)|"
    r"loading\b|please wait|one moment|hold on|"
    r"sure[!,.]|certainly[!,.]|of course[!,.]|great[!,.]|"
    r"here(?:'| i)s? (?:the|a|your)|as requested"
    r")",
    re.IGNORECASE,
)


def _is_meta_chatter(sentence: str) -> bool:
    """True when ``sentence`` is procedural narrator chatter, not content."""
    s = (sentence or "").strip()
    if not s:
        return True
    return bool(_CHATTER_RE.match(s))


def _split_sentences(text: str) -> list[str]:
    """Split prose into sentences, stripping markdown artifacts."""
    parts = re.split(r"(?<=[.!?。！？])\s+", (text or "").strip())
    return [re.sub(r"[#*_>`]", "", p).strip() for p in parts if p and p.strip()]


def _prose_to_summary(text: str, max_chars: int = 4000) -> str:
    """Condense assistant prose into a ReportCard summary string.

    Procedural chatter sentences (``_is_meta_chatter``) are dropped first so
    the summary carries actual content rather than the assistant narrating
    its own process.
    """
    if not text:
        return ""
    sentences = [s for s in _split_sentences(text) if not _is_meta_chatter(s)]
    body = re.sub(r"\s+", " ", " ".join(sentences)).strip()
    if not body:
        return ""
    if len(body) > max_chars:
        truncated = body[: max_chars - 1]
        # Prefer breaking on a word boundary, but never exceed max_chars.
        if " " in truncated:
            truncated = truncated.rsplit(" ", 1)[0]
        body = truncated + "…"
    return body


def _title_from_prose(text: str, fallback: str) -> str:
    """Best-effort title: first markdown H1, else first *non-chatter*
    sentence, else fallback."""
    if text:
        h1 = re.search(r"^\s*#\s+(.+)$", text, flags=re.MULTILINE)
        if h1:
            return h1.group(1).strip()[:120]
        for sentence in _split_sentences(text):
            if _is_meta_chatter(sentence):
                continue
            return sentence[:120]
    return fallback


def _title_from_user_request(user_message: str, fallback: str) -> str:
    """Derive a report title from the user's own request — it states intent
    without narrator chatter, so it beats assistant prose as a title source."""
    text = re.sub(r"\s+", " ", (user_message or "").strip())
    if not text:
        return fallback
    text = re.split(r"(?<=[.!?。！？])\s", text, maxsplit=1)[0].strip()
    return text[:120] if text else fallback


def _mine_ask_data_result(tool_calls_for_frontend: list[dict]) -> Optional[dict]:
    """Return the richest ``ask_data_agent`` result dict from this turn.

    Prefers a result carrying this turn's synthesized ``report_card_payload``;
    otherwise the last result that has rows and/or an answer. Returns None
    when no data call happened this turn.
    """
    if not tool_calls_for_frontend:
        return None
    best: Optional[dict] = None
    for tc in tool_calls_for_frontend:
        if tc.get("name") != "ask_data_agent":
            continue
        res = tc.get("results") or {}
        if not isinstance(res, dict):
            continue
        if isinstance(res.get("report_card_payload"), dict) and res["report_card_payload"]:
            return res  # jackpot: this turn's own synthesized payload
        if res.get("rows") or res.get("answer"):
            best = res
    return best


def _mine_ask_data_rows(tool_calls_for_frontend: list[dict]) -> list[dict]:
    """Pull row dicts out of any ``ask_data_agent`` tool call in this turn.

    Honors the cap the platform documents for embedded data: anything beyond
    ``_DASHBOARD_MAX_ROWS`` is dropped (a fully pre-aggregated sample is
    preferable to a 100 MB HTML file).
    """
    if not tool_calls_for_frontend:
        return []
    rows: list[dict] = []
    for tc in tool_calls_for_frontend:
        if tc.get("name") != "ask_data_agent":
            continue
        res = tc.get("results") or {}
        if not isinstance(res, dict):
            continue
        # ask_data_agent returns {"answer": ..., "rows": [...], "sql": ...,
        # "source_name": ...}. The results can also be nested under
        # "data" or "result" depending on the call site; accept both.
        candidate = res.get("rows")
        if not isinstance(candidate, list):
            nested = res.get("data") or res.get("result")
            if isinstance(nested, dict):
                candidate = nested.get("rows")
        if not isinstance(candidate, list):
            continue
        for r in candidate:
            if isinstance(r, dict):
                rows.append(r)
            if len(rows) >= _DASHBOARD_MAX_ROWS:
                return rows
    return rows


def _rows_to_kpis(rows: list[dict], max_kpis: int = 4) -> list[dict]:
    """Pick up to ``max_kpis`` numeric columns and compute sum/avg/max KPIs.

    Each KPI is ``{label, value, display}``. Columns are scanned in stable
    column order (insertion order of the first row) so the dashboard reads
    deterministically. Up to two aggregates per column (sum + max) keeps the
    tile count small even with many numeric columns.
    """
    if not rows:
        return []
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                cols.append(k)
    kpis: list[dict] = []
    for col in cols:
        nums: list[tuple[Any, float]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            v = r.get(col)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                nums.append((r.get(col), float(v)))
            elif isinstance(v, str):
                # accept "1,234.56" and "12%" shapes
                cleaned = v.replace(",", "").rstrip("%").strip()
                try:
                    nums.append((v, float(cleaned)))
                except ValueError:
                    pass
        if not nums:
            continue
        s = sum(n for _, n in nums)
        m = max(nums, key=lambda x: x[1])
        unit = "%" if isinstance(m[0], str) and str(m[0]).endswith("%") else ""
        kpis.append({
            "label": f"Total {col}",
            "value": s,
            "display": f"{s:,.2f}{unit}" if abs(s) >= 1 else f"{s:.4f}{unit}",
        })
        if len(kpis) >= max_kpis:
            break
        kpis.append({
            "label": f"Max {col}",
            "value": m[1],
            "display": f"{m[1]:,.2f}{unit}" if abs(m[1]) >= 1 else f"{m[1]:.4f}{unit}",
        })
        if len(kpis) >= max_kpis:
            break
    return kpis[:max_kpis]


def _pick_chart_columns(rows: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """Choose (label_col, value_col) for a single Chart.js bar chart.

    Heuristic: first non-numeric column as labels, first numeric column as
    values. Returns ``(None, None)`` if no suitable pair is found.
    """
    if not rows:
        return None, None
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                cols.append(k)
    label_col: Optional[str] = None
    value_col: Optional[str] = None
    for col in cols:
        sample = next((r.get(col) for r in rows if isinstance(r, dict)), None)
        if sample is None:
            continue
        if isinstance(sample, (int, float)) and not isinstance(sample, bool):
            if value_col is None:
                value_col = col
        elif label_col is None:
            label_col = col
        if label_col and value_col:
            break
    return label_col, value_col


def _dashboard_theme_vars(theme) -> dict[str, str]:
    """Map a DeckTheme onto the dashboard's CSS variables.

    Falls back to the legacy dark dashboard palette when ``theme`` is
    None (no brand kit configured), so existing output is unchanged.
    """
    if theme is None:
        return {
            "bg": "#0f172a", "panel": "#111827", "ink": "#e5e7eb",
            "muted": "#94a3b8", "accent": "#60a5fa", "grid": "#1f2937",
        }
    h = theme.as_hex_dict()
    return {
        "bg": h["slide_bg"],
        "panel": h["surface"],
        "ink": h["text"],
        "muted": h["muted"],
        "accent": h["primary"],
        "grid": h["border"],
    }


def _dashboard_chart_type(labels: list[str]) -> str:
    """Pick a chart type from the data shape (P2.2 chart intelligence).

    * date-like labels (ISO dates, year-month, quarter) → line
    * everything else → bar (ranking/category comparisons)
    """
    import re as _re

    date_pat = _re.compile(
        r"^(\d{4}[-/]\d{1,2}([-/]\d{1,2})?|\d{1,2}[-/]\d{4}|\d{4}\s?[Qq][1-4]|"
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})"
    )
    if labels:
        hits = sum(1 for l in labels[:12] if date_pat.match(str(l).strip()))
        if hits >= max(1, len(labels[:12]) // 2):
            return "line"
    return "bar"


def _synthesize_dashboard_html(
    title: str,
    rows: list[dict],
    prose_summary: str,
    source_label: str = "ask_data_agent",
    theme=None,
) -> str:
    """Build a self-contained interactive HTML dashboard from query rows.

    Always returns a valid, browser-renderable HTML document. When ``rows``
    is empty, still produces an "empty state" shell (title + prose summary
    + an "no data" panel) so the user gets a visible artifact rather than
    silence. Chart.js is loaded from jsDelivr with an SRI hash; styles
    follow the chart.js + CSS Grid card layout that the
    ``artifacts-builder`` skill recommends.

    P1.1 unified tokens: when ``theme`` (a DeckTheme, e.g. resolved from
    the tenant brand kit) is provided, the dashboard palette comes from
    the same design tokens as the PPTX/DOCX renderers instead of the
    hardcoded dark palette.
    """
    safe_title = _html.escape((title or "Dashboard")[:_DASHBOARD_TITLE_MAX])
    kpis = _rows_to_kpis(rows)
    label_col, value_col = _pick_chart_columns(rows)
    chart_payload: dict[str, Any] = {"labels": [], "values": []}
    if rows and label_col and value_col:
        chart_payload = {
            "labels": [str(r.get(label_col, "")) for r in rows[:50] if isinstance(r, dict)],
            "values": [
                float(r.get(value_col, 0)) if isinstance(r.get(value_col, 0), (int, float))
                else 0.0
                for r in rows[:50] if isinstance(r, dict)
            ],
            "label_col": label_col,
            "value_col": value_col,
        }
    embedded_rows = [
        {str(k): _html.escape(str(v)[:200]) for k, v in r.items() if isinstance(r, dict)}
        for r in rows[:_DASHBOARD_MAX_ROWS]
    ]
    embedded_json = json.dumps({
        "rows": embedded_rows,
        "source": source_label,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kpis": kpis,
        "chart": chart_payload,
    }, ensure_ascii=False, default=str)

    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-label">{_html.escape(k["label"])}</div>'
        f'<div class="kpi-value">{_html.escape(k["display"])}</div></div>'
        for k in kpis
    ) or '<div class="kpi"><div class="kpi-label">No data</div><div class="kpi-value">—</div></div>'

    # Build the table (≤50 rows for initial render; full data is in
    # the embedded JSON for the "show all" toggle below).
    if embedded_rows and rows:
        header_cells = "".join(f"<th>{_html.escape(c)}</th>" for c in embedded_rows[0].keys())
        body_rows = "".join(
            "<tr>" + "".join(f"<td>{v}</td>" for v in r.values()) + "</tr>"
            for r in embedded_rows[:50]
        )
        table_html = (
            f'<table id="dash-table"><thead><tr>{header_cells}</tr></thead>'
            f'<tbody>{body_rows}</tbody></table>'
        )
    else:
        _no_data_msg = (
            prose_summary
            or "No rows returned by the query. Ask the agent to relax the filter "
               "or pick a different source."
        )
        table_html = (
            f'<div class="empty">{_html.escape(_no_data_msg)}</div>'
        )

    has_chart = bool(chart_payload.get("labels"))
    chart_block = (
        f'<div class="chart-wrap"><canvas id="dash-chart" '
        f'aria-label="Chart of {safe_title}"></canvas></div>'
        if has_chart else ""
    )

    # Unified design tokens (P1.1) + data-driven chart type (P2.2).
    tv = _dashboard_theme_vars(theme)
    chart_type = _dashboard_chart_type(chart_payload.get("labels") or [])
    accent_rgb = tuple(int(tv["accent"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{safe_title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"
        integrity="sha384-8dbf0940c6cca015338166ad7dee823800a2da58dc0dd650d8ec5ccc60376c635e59efba0c284c4c125e99e77b667bc9"
        crossorigin="anonymous"></script>
<style>
  :root {{ --bg: {tv["bg"]}; --panel: {tv["panel"]}; --ink: {tv["ink"]}; --muted: {tv["muted"]};
          --accent: {tv["accent"]}; --grid: {tv["grid"]}; --radius: 12px; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 24px; background: var(--bg); color: var(--ink);
         font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, sans-serif; }}
  h1 {{ margin: 0 0 4px; font-size: 22px; font-weight: 600; }}
  .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 18px; }}
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
          gap: 12px; margin-bottom: 18px; }}
  .kpi {{ background: var(--panel); border: 1px solid var(--grid); border-radius: var(--radius);
         padding: 14px 16px; }}
  .kpi-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase;
               letter-spacing: 0.05em; }}
  .kpi-value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
  .chart-wrap, .table-wrap {{ background: var(--panel); border: 1px solid var(--grid);
                             border-radius: var(--radius); padding: 16px; margin-bottom: 18px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--grid); }}
  th {{ color: var(--muted); font-weight: 500; cursor: pointer; user-select: none; }}
  tr:hover td {{ background: rgba(96,165,250,0.08); }}
  .empty {{ padding: 28px; text-align: center; color: var(--muted); }}
  .toolbar {{ display: flex; gap: 8px; align-items: center; margin-bottom: 10px;
             color: var(--muted); font-size: 12px; }}
  .toolbar input {{ background: var(--bg); color: var(--ink); border: 1px solid var(--grid);
                   border-radius: 6px; padding: 4px 8px; }}
  @media print {{ body {{ background: #fff; color: #000; }} .kpi, .chart-wrap, .table-wrap
                 {{ background: #fff; border-color: #ddd; }} }}
</style>
</head>
<body>
<h1>{safe_title}</h1>
<div class="meta">Source: {_html.escape(source_label)} · generated {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</div>
<div class="kpis">{kpi_html}</div>
{chart_block}
<div class="table-wrap">
  <div class="toolbar">Filter: <input id="dash-filter" placeholder="Search rows…"></div>
  {table_html}
</div>
<script id="dashboard-data" type="application/json">{embedded_json}</script>
<script>
(function () {{
  const data = JSON.parse(document.getElementById('dashboard-data').textContent);
  const hasChart = {str(has_chart).lower()};
  if (hasChart && window.Chart) {{
    const ctx = document.getElementById('dash-chart').getContext('2d');
    const ACCENT = 'rgba({accent_rgb[0]},{accent_rgb[1]},{accent_rgb[2]},1)';
    const ACCENT_SOFT = 'rgba({accent_rgb[0]},{accent_rgb[1]},{accent_rgb[2]},0.6)';
    new Chart(ctx, {{
      type: '{chart_type}',
      data: {{
        labels: data.chart.labels,
        datasets: [{{ label: data.chart.value_col, data: data.chart.values,
                     backgroundColor: ACCENT_SOFT, borderColor: ACCENT, borderWidth: 1,
                     fill: {str(chart_type == "line").lower()}, tension: 0.25 }}]
      }},
      options: {{ responsive: true, plugins: {{ legend: {{ labels: {{ color: '{tv["ink"]}' }} }} }},
                  scales: {{ x: {{ ticks: {{ color: '{tv["muted"]}' }} }},
                             y: {{ ticks: {{ color: '{tv["muted"]}' }}, grid: {{ color: '{tv["grid"]}' }} }} }} }}
    }});
  }}
  const filter = document.getElementById('dash-filter');
  if (filter) {{
    filter.addEventListener('input', function () {{
      const q = this.value.toLowerCase();
      document.querySelectorAll('#dash-table tbody tr').forEach(function (tr) {{
        tr.style.display = tr.textContent.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
      }});
    }});
  }}
  document.querySelectorAll('#dash-table th').forEach(function (th, idx) {{
    th.addEventListener('click', function () {{
      const tbody = th.closest('table').querySelector('tbody');
      const rows = Array.from(tbody.querySelectorAll('tr'));
      const asc = th.dataset.asc !== '1';
      rows.sort(function (a, b) {{
        const av = a.children[idx].textContent, bv = b.children[idx].textContent;
        const an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      }});
      rows.forEach(function (r) {{ tbody.appendChild(r); }});
      th.dataset.asc = asc ? '1' : '0';
    }});
  }});
}})();
</script>
</body>
</html>
"""


async def ensure_artifact_for_doc_request(
    doc_format: Optional[FileFormat],
    assistant_content: str,
    already_created: list[dict],
    tool_calls_for_frontend: list[dict],
    db: Session,
    context: dict,
    artifact_ids: Optional[list] = None,
) -> Optional[dict]:
    """Guarantee an artifact exists when the user asked for a file.

    Only acts when ALL of the following hold:
      * ``doc_format`` is a renderable artifact type (docx/pptx/pdf/html);
      * no marker already produced an artifact (``already_created`` empty);
      * no successful ``create_artifact`` / ``run_sandbox_skill`` tool call
        is already recorded;
      * the data-path finalize did not already attach an artifact
        (``artifact_ids`` empty).

    Returns the created artifact result dict, or ``None`` if no fallback was
    needed or possible.
    """
    if not doc_format:
        return None
    artifact_type = _FORMAT_TO_ARTIFACT_TYPE.get(doc_format)
    if artifact_type is None:
        # xlsx / md / dashboard — not renderable by the current pipeline.
        logger.info(
            "orchestrator: doc_format=%s has no artifact renderer; skipping fallback",
            doc_format,
        )
        return None

    if already_created:
        if not settings.GOAL_CONTRACT_ENABLED:
            return None  # a marker already produced one (legacy)
        # Contract mode: only an artifact of the requested kind with a
        # non-empty, non-failed payload counts as satisfaction. A report card
        # or a failed artifact never satisfies an explicit doc deliverable.
        expected = _FORMAT_TO_ARTIFACT_TYPE.get(doc_format)
        if any(
            isinstance(a, dict)
            and bool(a.get("success"))
            and expected is not None
            and (a.get("type") or "").lower().strip() == expected
            for a in already_created
        ):
            return None
        # None matched — fall through so the user still gets the deliverable.

    if artifact_ids:
        if not settings.DOC_REQUEST_STRICT_ARTIFACT_MATCH_ENABLED:
            # Legacy behavior: the ask_data_agent finalize path (rich or
            # no-data) already produced an artifact this turn — creating
            # another would be a user-visible duplicate.
            return None
        # Strict mode: only artifacts whose stored type matches the
        # requested format count as satisfaction. A report card (html_report)
        # never satisfies an explicit pptx/docx/xlsx/dashboard request.
        if settings.GOAL_CONTRACT_ENABLED:
            # Contract mode adds the content gate: the payload must actually
            # exist (non-empty, non-failed build) — not just match by type.
            if any(
                _artifact_satisfies_deliverable(aid, doc_format, db)
                for aid in artifact_ids
            ):
                return None
        else:
            if any(
                _artifact_matches_requested_format(aid, doc_format, db)
                for aid in artifact_ids
            ):
                return None
        # None matched — fall through to the fallback so the user still gets
        # the deliverable they asked for.

    for tc in tool_calls_for_frontend or []:
        if tc.get("name") not in ("create_artifact", "run_sandbox_skill"):
            continue
        res = tc.get("results") or {}
        if isinstance(res, dict) and res.get("success") and res.get("artifact_id"):
            return None  # an artifact engine already made one

    # Dashboard fallback: mine ask_data_agent rows (if any) and synthesize a
    # real interactive HTML dashboard. This is the guarantee path: the user
    # asked for a dashboard, so the user gets one — even when the LLM
    # produced neither a DASHBOARD marker nor a create_artifact tool call.
    if doc_format == "dashboard":
        rows = _mine_ask_data_rows(tool_calls_for_frontend or [])
        title = _title_from_prose(assistant_content, fallback="Dashboard")
        # If prose is empty but rows are present, use a content-derived title.
        if not assistant_content.strip() and rows:
            cols = list(rows[0].keys()) if isinstance(rows[0], dict) else []
            title = f"Dashboard — {', '.join(cols[:3])}" if cols else "Dashboard"
        # P1.1/P1.2: resolve the tenant brand kit so the dashboard shares
        # the same design tokens as decks/docs (falls back to the legacy
        # dark palette when no kit is configured).
        dash_theme = None
        try:
            from app.services.artifacts.brand_kit import get_brand_kit
            from app.services.artifacts.exporters._theme import theme_from_brand_kit

            kit = get_brand_kit(
                db,
                org_id=(context or {}).get("org_id") or "default-org",
                app_id=(context or {}).get("app_id") or "default-app",
            )
            dash_theme = theme_from_brand_kit(kit) if kit else None
        except Exception as _theme_exc:
            logger.debug("orchestrator: brand kit resolution skipped: %s", _theme_exc)
        html_content = _synthesize_dashboard_html(
            title=title,
            rows=rows,
            prose_summary=_prose_to_summary(assistant_content, max_chars=400),
            source_label="ask_data_agent + orchestrator fallback" if rows else "orchestrator fallback",
            theme=dash_theme,
        )
        payload = {
            "title": title,
            "html_content": html_content,
            "source": "orchestrator-fallback",
            "user_signal": "dashboard",
            "row_count": len(rows),
        }
        try:
            result = await _create_artifact_tool(
                args={
                    "type": "html",
                    "title": title,
                    "payload": payload,
                    "skill": "dashboard",
                    "description": (
                        f"Auto-generated dashboard from {len(rows)} query row(s) "
                        "(orchestrator fallback)."
                    ),
                },
                db=db,
                context=context,
            )
            if isinstance(result, dict) and result.get("success"):
                logger.info(
                    "orchestrator: dashboard fallback created html artifact %s "
                    "(rows=%d, doc_format=%s)",
                    result.get("artifact_id"), len(rows), doc_format,
                )
                return result
            logger.warning(
                "orchestrator: dashboard fallback artifact creation failed: %s",
                (result or {}).get("error") if isinstance(result, dict) else result,
            )
        except Exception as exc:
            logger.warning(
                "orchestrator: dashboard fallback raised (non-fatal): %s", exc,
            )
        return None

    # Fallback: build the export payload data-first.
    #
    # Priority order:
    #   1. This turn's own synthesized report-card payload (richest — the
    #      in-chat card and the exported file then tell the same story);
    #   2. This turn's raw ask_data rows (KPIs + chart + the agent's own
    #      natural-language answer as the summary);
    #   3. The assistant's prose, with narrator chatter filtered out.
    #
    # The title prefers the turn payload, then the user's request, then
    # non-chatter prose — never "I'm going to build …" meta-sentences.
    user_message = (context or {}).get("user_message") or ""
    fallback_title = f"{doc_format}-export"
    data_result = _mine_ask_data_result(tool_calls_for_frontend or [])

    payload: dict[str, Any] | None = None
    title: Optional[str] = None

    turn_rcp = (data_result or {}).get("report_card_payload")
    if isinstance(turn_rcp, dict) and turn_rcp:
        # Reuse this turn's payload verbatim so the exported file mirrors
        # the in-chat report card (including an honest "no data" story).
        payload = dict(turn_rcp)
        title = payload.get("title") or None

    if payload is None:
        rows = _mine_ask_data_rows(tool_calls_for_frontend or [])
        answer = ((data_result or {}).get("answer") or "").strip()
        summary = answer or _prose_to_summary(assistant_content)
        payload = {
            "summary": summary,
            "source": (data_result or {}).get("source_name") or "assistant-reply",
            "user_signal": "export",
        }
        if rows:
            kpis = _rows_to_kpis(rows)
            if kpis:
                payload["kpis"] = kpis
            label_col, value_col = _pick_chart_columns(rows)
            if label_col and value_col:
                payload["chart"] = {
                    "type": "bar",
                    "title": f"{value_col} by {label_col}",
                    "x_key": label_col,
                    "y_keys": [value_col],
                    "data": rows[:50],
                }
        sql = ((data_result or {}).get("sql") or "").strip()
        if sql:
            payload["sql"] = sql

    if not title:
        title = _title_from_user_request(user_message, fallback="")
    if not title:
        title = _title_from_prose(assistant_content, fallback=fallback_title)
    payload["title"] = title
    if not payload.get("summary"):
        payload["summary"] = title
    payload.setdefault("user_signal", "export")

    try:
        result = await _create_artifact_tool(
            args={
                "type": artifact_type,
                "title": title,
                "payload": payload,
                "skill": str(doc_format),
                "description": "Auto-generated from assistant reply (orchestrator fallback).",
            },
            db=db,
            context=context,
        )
        if isinstance(result, dict) and result.get("success"):
            logger.info(
                "orchestrator: fallback created %s artifact %s (doc_format=%s)",
                artifact_type, result.get("artifact_id"), doc_format,
            )
            return result
        logger.warning(
            "orchestrator: fallback artifact creation failed: %s",
            (result or {}).get("error") if isinstance(result, dict) else result,
        )
    except Exception as exc:
        logger.warning("orchestrator: fallback artifact creation raised (non-fatal): %s", exc)
    return None


__all__ = [
    "fulfill_markers",
    "ensure_artifact_for_doc_request",
]
