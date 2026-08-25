"""artifact_tool — create and edit downloadable documents (docx, pdf, pptx, html).

Two LLM-callable tools for document lifecycle:

``create_artifact``
  After loading a skill's methodology via ``skills.execute(...)``, the
  agent calls this tool to produce a file.  The pipeline:

  1. Create an Artifact row (draft)
  2. Create a version row (building)
  3. Render the file bytes via the format exporter
  4. Store the original blob
  5. Run preview_builder.convert_to_preview() → store preview blob
  6. Mark version as preview_ready
  7. Return {artifact_id, version, file_url, preview_url, ...}

``edit_artifact``
  Refine an existing artifact via natural-language instructions (e.g.
  "make the title bolder", "add a summary slide").  The pipeline:

  1. Look up the artifact and its current version's source_json
  2. Call LLM to interpret the edit instructions → produce modified payload
  3. Create a new version with the modified payload (version-bumps)
  4. Re-render the file bytes and store the new blob
  5. Return updated artifact info with new version_number

The message-linking and artifact[] surface on the assistant message are
handled by the post-processing step in agents.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.session_state import SessionState
from app.services.artifacts.event_logger import log_deck_event_fire_and_forget
from app.services.artifacts.exporters import ExportContext
from app.services.artifacts.exporters._common import chart_rows
from app.services.data_execution.execution_service import DataExecutionService
from app.services.data_execution.session_state import SessionStateService
from app.services.tool_handlers._errors import ToolExecutionError
from app.services.tool_handlers._payload_from_execution import _payload_from_execution
from app.services.tool_registry import registry
from app.services.synexia.contracts import ReportCardPayload

logger = logging.getLogger(__name__)

# Ceiling for long synchronous file-format renders / preview conversions
# (PPTX/DOCX/PDF rendering can take 10-120s). These run via
# asyncio.to_thread so the event loop stays free to pump the SSE heartbeat;
# anything slower than the ceiling is treated as a stall we fail over
# instead of blocking the stream (which previously dropped the connection).
ARTIFACT_RENDER_TIMEOUT_S = 120


async def _run_blocking(fn, *args, **kwargs):
    """Run a long synchronous call off the event loop, bounded by a timeout.

    File-format renders / preview conversions (10-120s) must never be awaited
    directly on the loop: they would block the SSE heartbeat and the browser
    would drop the stream ("connection interrupted"). ``asyncio.to_thread``
    keeps the loop free to pump heartbeats, and ``asyncio.wait`` bounds the
    wait so a stalled render raises ``asyncio.TimeoutError`` (caught by
    callers to fail over gracefully). Note a timed-out worker thread keeps
    running in the background (threads cannot be killed), but the loop is
    never blocked.
    """
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    done, _ = await asyncio.wait({task}, timeout=ARTIFACT_RENDER_TIMEOUT_S)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise asyncio.TimeoutError(
            f"blocking call timed out after {ARTIFACT_RENDER_TIMEOUT_S}s"
        )
    return task.result()


# Valid types and the corresponding exporters / handlers
_ARTIFACT_TYPE_TO_EXPORT_FORMAT = {
    "docx": "docx",
    "pdf": "pdf",
    "pptx": "pptx",
}

# MIME types and file extensions per type
_TYPE_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "html": "text/html",
}

_TYPE_EXT = {
    "docx": ".docx",
    "pdf": ".pdf",
    "pptx": ".pptx",
    "html": ".html",
}


def _safe_filename(title: str, ext: str) -> str:
    """Make a safe file name from a title, preserving Unicode characters."""
    import re
    # Remove characters that are unsafe in most file systems
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip() or "untitled"
    base = re.sub(r"\s+", "_", base)
    return f"{base}{ext}"


def _render_sidecar_html(rcp: ReportCardPayload) -> bytes:
    """Render a rich interactive HTML sidecar from a ReportCardPayload.

    Used to populate the inline-preview for non-HTML artifacts (docx,
    pptx, xlsx) so the user sees the same "rich dashboard" preview as
    the HTML report.  Self-contained — no external CDN/JS so it
    always renders.

    The rendered page includes (in order):
    1. Hero with title, source, generation timestamp
    2. Executive summary (rcp.summary) and/or methodology (rcp.methodology)
    3. KPI tiles
    4. Key findings (rcp.key_findings) — narrative paragraphs
    5. Insights (rcp.insights) — bulleted list
    6. Data table (chart rows)
    7. Recommendations (rcp.recommendations) — bulleted list
    8. Custom sections (rcp.sections) — heading + paragraph/bullets
    9. SQL block (rcp.sql)
    10. Footer

    (rcp.next_step is deliberately excluded — it is conversational guidance
    for the in-chat card, not report content.)
    """
    # --- Data table (used in section 6) --------------------------------
    rows = rcp.chart.data if rcp.chart else []
    if rows and rcp.chart:
        headers = list(rows[0].keys())
        head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{_esc(str(r.get(h, '')))}</td>" for h in headers) + "</tr>"
            for r in rows[:200]
        )
        table_html = (
            f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        )
    else:
        table_html = "<p class='muted'><em>No tabular data.</em></p>"

    # --- KPI tiles (section 3) -----------------------------------------
    kpi_html = "".join(
        f"<div class='kpi'><div class='label'>{_esc(k.label)}</div>"
        f"<div class='value'>{_esc(k.value)}</div>"
        + (f"<div class='delta'>{_esc(k.delta)}</div>" if k.delta else "")
        + (f"<div class='caption'>{_esc(k.caption)}</div>" if k.caption else "")
        + "</div>"
        for k in rcp.kpis
    )

    # --- Section helpers ------------------------------------------------
    def _insights_ul(items) -> str:
        if not items:
            return ""
        return "<ul>" + "".join(f"<li>{_esc(i.text)}</li>" for i in items) + "</ul>"

    def _findings_html(items) -> str:
        """Key findings as paragraphs (no bullets) so they read as prose."""
        if not items:
            return ""
        paras = "".join(f"<p>{_esc(f.text)}</p>" for f in items)
        return f"<div class='findings'>{paras}</div>"

    def _section_html(sec) -> str:
        parts = [f"<h2>{_esc(sec.title)}</h2>"]
        if sec.content:
            parts.append(f"<p>{_esc(sec.content)}</p>")
        if sec.bullets:
            parts.append("<ul>" + "".join(
                f"<li>{_esc(b)}</li>" for b in sec.bullets
            ) + "</ul>")
        return "<div class='section'>" + "".join(parts) + "</div>"

    summary = rcp.summary or ""
    methodology = rcp.methodology or ""
    source = rcp.source or "unknown"

    # --- Compose the page -----------------------------------------------
    parts: list[str] = []
    parts.append("<!doctype html><html><head><meta charset='utf-8'>")
    parts.append(f"<title>{_esc(rcp.title)}</title>")
    parts.append(_sidecar_css())
    parts.append("</head><body><div class='wrap'>")
    # 1. Hero
    parts.append(
        f"<div class='hero'><h1>{_esc(rcp.title)}</h1>"
        f"<div class='meta'>Source: {_esc(source)} · "
        f"Generated {_esc(rcp.generated_at or '')}</div></div>"
    )
    # 2a. Executive summary
    if summary:
        parts.append(
            f"<div class='callout'><span class='badge'>EXECUTIVE SUMMARY</span>"
            f"<p>{_esc(summary)}</p></div>"
        )
    # 2b. Methodology (rendered as a callout if present)
    if methodology:
        parts.append(
            f"<div class='callout subtle'><span class='badge subtle'>METHODOLOGY</span>"
            f"<p>{_esc(methodology)}</p></div>"
        )
    # 3. KPI tiles
    if rcp.kpis:
        parts.append(f"<div class='kpis'>{kpi_html}</div>")
    # 4. Key findings (narrative paragraphs)
    if rcp.key_findings:
        parts.append("<div class='section'>"
                     "<h2>Key Findings</h2>"
                     f"{_findings_html(rcp.key_findings)}"
                     "</div>")
    # 5. Insights (bullets)
    if rcp.insights:
        parts.append("<div class='section'>"
                     "<h2>Insights</h2>"
                     f"{_insights_ul(rcp.insights)}"
                     "</div>")
    # 6. Data table
    parts.append(f"<div class='section'><h2>Data</h2>{table_html}</div>")
    # 7. Recommendations
    if rcp.recommendations:
        parts.append("<div class='section'>"
                     "<h2>Recommendations</h2>"
                     f"{_insights_ul(rcp.recommendations)}"
                     "</div>")
    # 8. Custom sections
    for sec in rcp.sections:
        parts.append(_section_html(sec))
    # 9. SQL block  (next_step is intentionally omitted — see docstring)
    if rcp.sql:
        parts.append(
            "<div class='section'><h2>SQL</h2>"
            f"<pre><code>{_esc(rcp.sql)}</code></pre></div>"
        )
    # 11. Footer
    parts.append(
        "<footer>Zhanlu report · Generated by Zhanlu AI</footer>"
        "</div></body></html>"
    )
    return "".join(parts).encode("utf-8")


def _sidecar_css() -> str:
    """Reusable CSS block for the sidecar HTML.  Kept as its own
    helper so unit tests can verify the structure and so the
    docx/pptx/xlsx paths share the same look-and-feel."""
    return (
        "<style>"
        "  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        "       margin:0;color:#0F172A;background:#F8FAFC;}"
        "  .wrap{max-width:1040px;margin:0 auto;padding:32px;}"
        "  .hero{background:linear-gradient(135deg,#0F172A 0%,#1E3A8A 100%);"
        "        color:#fff;border-radius:14px;padding:28px 32px;margin-bottom:24px;}"
        "  .hero h1{font-size:26px;margin:0 0 6px;font-weight:700;}"
        "  .hero .meta{opacity:.85;font-size:13px;}"
        "  .callout{background:#FEF3C7;border-left:4px solid #F59E0B;"
        "           padding:14px 18px;border-radius:6px;margin:0 0 24px;}"
        "  .callout.subtle{background:#F1F5F9;border-left-color:#64748B;}"
        "  .badge{display:inline-block;background:#92400E;color:#fff;"
        "         font-size:10px;font-weight:700;letter-spacing:1px;"
        "         padding:3px 8px;border-radius:4px;margin-bottom:6px;}"
        "  .badge.subtle{background:#475569;}"
        "  .callout p{margin:6px 0 0;line-height:1.55;}"
        "  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));"
        "        gap:14px;margin-bottom:24px;}"
        "  .kpi{background:#fff;border:1px solid #E2E8F0;border-radius:10px;padding:16px;"
        "       box-shadow:0 1px 2px rgba(15,23,42,0.04);}"
        "  .kpi .label{font-size:11px;color:#64748B;text-transform:uppercase;"
        "              letter-spacing:.5px;font-weight:600;}"
        "  .kpi .value{font-size:28px;font-weight:700;color:#0F172A;margin-top:4px;}"
        "  .kpi .delta{display:inline-block;font-size:12px;font-weight:600;"
        "              color:#059669;background:#D1FAE5;padding:2px 6px;"
        "              border-radius:4px;margin-top:4px;}"
        "  .kpi .caption{font-size:12px;color:#94A3B8;margin-top:2px;}"
        "  .section{background:#fff;border:1px solid #E2E8F0;border-radius:10px;"
        "           padding:20px 24px;margin-bottom:16px;}"
        "  .section h2{font-size:18px;margin:0 0 12px;color:#0F172A;}"
        "  .section p{line-height:1.6;margin:8px 0;}"
        "  .section ul{margin:0;padding-left:18px;}"
        "  .section li{margin:6px 0;line-height:1.55;}"
        "  .findings p{margin:10px 0;line-height:1.65;color:#1E293B;}"
        "  table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px;}"
        "  th{background:#F1F5F9;text-align:left;padding:8px 10px;"
        "     color:#475569;font-weight:600;}"
        "  td{padding:8px 10px;border-top:1px solid #E2E8F0;}"
        "  tr:nth-child(even) td{background:#FAFBFC;}"
        "  pre{background:#0F172A;color:#E2E8F0;padding:12px 14px;"
        "       border-radius:6px;overflow-x:auto;font-size:12px;}"
        "  .muted{color:#94A3B8;}"
        "  footer{font-size:11px;color:#94A3B8;margin-top:24px;text-align:center;}"
        "</style>"
    )


def _esc(s: object) -> str:
    """HTML-escape a string for safe inline insertion."""
    import html as _html
    return _html.escape(str(s), quote=True)


def _enrich_payload_from_sibling_html_report(
    *,
    payload: dict,
    db,
    conversation_id: str,
    title: str,
) -> dict:
    """Fill in Claude-style content fields on ``payload`` from a
    sibling ``html_report`` artifact in the same conversation.

    This is the ``create_artifact`` analogue of the
    ``run_sandbox_skill`` enrichment: when the agent turns an
    existing rich HTML report into a downloadable file, the rich
    ``ReportCardPayload`` lives on the ``html_report`` artifact's
    ``source_json`` but the agent's ``create_artifact`` payload is
    often just ``{html_path, filename}``.  We copy the rich
    fields so the docx/pptx/pdf renders as a Claude-style report
    instead of a title + data table.
    """
    try:
        from app.models.artifact import Artifact, ArtifactVersion
        from sqlalchemy import desc

        # Same lookup strategy as the run_sandbox_skill enrichment:
        # accept either a rich ``html_report`` (from
        # ``finalize_into_artifact``) or a rich ``html`` sidecar
        # (from the sandbox's ``generate_html``).  The rcp lives
        # in different places for each — see below.
        siblings = (
            db.query(Artifact)
            .filter(
                Artifact.conversation_id == conversation_id,
                Artifact.artifact_type.in_(("html_report", "html")),
                Artifact.is_deleted == False,
            )
            .order_by(desc(Artifact.created_date))
            .all()
        )
        if not siblings:
            return payload

        def _norm(t: str) -> str:
            import re as _re
            t = (t or "").strip().rstrip(" (preview)").strip()
            t = _re.sub(r"\.(docx|pptx|xlsx|pdf|md|html|htm)$", "", t, flags=_re.IGNORECASE)
            return _re.sub(r"\s+", " ", t.replace("_", " ")).lower().strip()

        a = _norm(title)

        def _matches(b: str) -> bool:
            if not a or not b:
                return False
            if a == b:
                return True
            if a.startswith(b) or b.startswith(a):
                return True
            aw, bw = a.split(), b.split()
            for n in (3, 2):
                if len(aw) >= n and len(bw) >= n and aw[:n] == bw[:n]:
                    return True
            sw = {w for w in aw if len(w) >= 3}
            shared = sum(1 for w in bw if w in sw)
            if shared >= 2:
                return True
            return False

        def _extract_rcp(art) -> dict | None:
            """Pull the rcp out of an artifact, returning a non-empty
            dict or None.  An empty rcp is treated as missing so we
            don't accidentally pick a sparse sidecar that just
            happens to match the title."""
            md = art.metadata_json or {}
            rcp = None
            if isinstance(md, dict):
                rcp = md.get("report_card_payload") or md.get("rcp")
            if not isinstance(rcp, dict) or not rcp:
                version = (
                    db.query(ArtifactVersion)
                    .filter(ArtifactVersion.id == art.current_version_id)
                    .first()
                )
                if version is not None:
                    src = version.source_json or {}
                    if isinstance(src, dict):
                        rcp = src.get("rcp") or src
            if not isinstance(rcp, dict):
                return None
            if not any(rcp.get(k) for k in (
                "summary", "methodology", "kpis", "insights",
                "key_findings", "recommendations", "sections",
                "next_step", "sql",
            )):
                return None
            return rcp

        sibling = next(
            (s for s in siblings if _matches(_norm(s.title)) and _extract_rcp(s)),
            None,
        )
        # NOTE: no title-less catch-all here. The previous fallback grabbed
        # *any* rich html_report in the conversation when no title matched,
        # which leaked KPIs/summary from an unrelated earlier turn into this
        # turn's export. A missing match must leave the payload untouched.
        if sibling is None:
            return payload

        rcp = _extract_rcp(sibling)
        if not isinstance(rcp, dict) or not rcp:
            return payload

        for k in (
            "summary",
            "methodology",
            "source",
            "sql",
            "next_step",
            "kpis",
            "insights",
            "key_findings",
            "recommendations",
            "sections",
        ):
            if not payload.get(k) and rcp.get(k):
                payload[k] = rcp[k]
        return payload
    except Exception:
        return payload


def _create_sidecar_preview(
    service,
    parent_artifact,
    rcp: ReportCardPayload,
    user_message: str = "",
    conversation_id: Optional[str] = None,
    agent_app_id: Optional[str] = None,
) -> Optional[str]:
    """Create a sibling HTML artifact that previews the file-format
    artifact in the chat inline-preview pane.

    Sets the parent artifact's ``metadata_json.preview_artifact_id`` so
    the dedup layer in ``_collect_artifact_results`` can pair the two
    cards and surface the rich HTML preview when the user opens the
    docx/pptx/xlsx card.  Returns the sidecar's artifact id, or
    ``None`` on any failure (the original file is still returned).
    """
    try:
        # 1. Build a sidecar-friendly title so the dedup layer can
        #    pair it with the file-format artifact by title when
        #    metadata hasn't propagated yet.
        sidecar_title = f"{rcp.title} (preview)"

        # 2. Create the sidecar artifact row.
        sidecar = service.create_artifact(
            artifact_type="html",
            title=sidecar_title,
            description=f"Rich preview for {parent_artifact.title}",
            conversation_id=conversation_id,
            created_by_agent_id=agent_app_id,
        )
        sidecar_version = service.create_version(
            artifact_id=sidecar.id,
            changelog="Auto-generated sidecar HTML preview",
            source_json={"_sidecar_of": parent_artifact.id, "rcp": rcp.model_dump(mode="json")},
            produced_by_skill="sidecar_preview",
        )
        if not sidecar_version:
            return None

        # 3. Render + store the HTML blob.
        html_bytes = _render_sidecar_html(rcp)
        service.store_blob(
            version_id=sidecar_version.id,
            blob_type="original",
            file_name=f"{_safe_filename(rcp.title, '.html')}",
            mime_type="text/html; charset=utf-8",
            data=html_bytes,
        )
        service.mark_version_built(sidecar_version.id)
        service.update_status(sidecar.id, "preview_ready")

        # 4. Link the parent artifact → sidecar via metadata_json.
        #    Re-fetch the parent to avoid stale ORM state.
        fresh_parent = service.get_artifact(parent_artifact.id)
        if fresh_parent is not None:
            md = dict(fresh_parent.metadata_json or {})
            md["preview_artifact_id"] = sidecar.id
            fresh_parent.metadata_json = md
            service.db.add(fresh_parent)
            service.db.flush()
            service.db.commit()

        logger.info(
            "Sidecar HTML preview %s created for %s artifact %s",
            sidecar.id, parent_artifact.artifact_type, parent_artifact.id,
        )
        return sidecar.id
    except Exception as exc:
        logger.exception(
            "Sidecar preview creation failed for artifact %s: %s",
            getattr(parent_artifact, "id", "?"), exc,
        )
        try:
            service.db.rollback()
        except Exception:
            pass
        return None


def _build_deck_export_context(
    exporter,
    artifact,
    *,
    conversation_id: Optional[str],
    user_message: str,
    org_id: str,
    app_id: str,
) -> tuple[ExportContext, Optional[str]]:
    """Build the deck-pipeline ExportContext + brand fingerprint for the tool.

    When a workspace brand kit exists, ``ctx.theme_tokens`` carries the flat
    hex palette (so the pipeline themes the deck to the customer brand) and the
    returned fingerprint keys the cached ``format_export`` blob — a kit edit
    then invalidates the stale deck.  ``theme_name`` stays a vendored base name
    because hex tokens don't pass ``validate_theme_name``; the pipeline
    resolves the actual theme via ``ExportService._resolved_theme_name``.
    """
    ctx = ExportContext(
        conversation_id=conversation_id,
        user_message=user_message,
    )
    theme_tokens, brand_fp = exporter._resolve_brand_tokens(
        artifact, org_id=org_id, app_id=app_id
    )
    if theme_tokens:
        ctx.theme_tokens = theme_tokens
        ctx.theme_name = "zhanlu-blue"
    return ctx, brand_fp


def _collect_rows_for_tool(
    db,
    rcp: Optional[ReportCardPayload],
    *,
    artifact=None,
    conversation_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    user_message: str = "",
    limit: int = 2000,
) -> list[dict]:
    """Ground a deck in the REAL query rows; fall back to the payload rows.

    When ``PPT_DECK_DATA_GROUNDING_ENABLED``, recover the rows the agent
    actually fetched during the conversation (ObservationRecords on the
    artifact's execution / conversation — e.g. the ERP data the user asked
    to report on).  If nothing is recoverable (e.g. the legacy non-FSM path
    with no ``Execution``), fall back to the LLM-authored
    ``payload.chart.data`` (historical behavior).
    """
    if not getattr(settings, "PPT_DECK_DATA_GROUNDING_ENABLED", False):
        return chart_rows(rcp) if rcp else []
    from app.services.artifacts.deck_data import collect_grounded_rows

    rows = collect_grounded_rows(
        db,
        artifact=artifact,
        conversation_id=conversation_id,
        execution_id=execution_id,
        user_message=user_message,
        limit=limit,
    )
    if not rows:
        rows = chart_rows(rcp) if rcp else []
    return rows


async def _create_artifact_tool(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    """Handler for the create_artifact LLM tool."""
    if db is None:
        return {"success": False, "error": "Database session is required"}

    artifact_type = (args.get("type") or "").lower().strip()
    title = (args.get("title") or "").strip()
    payload = args.get("payload") or {}
    description = args.get("description") or ""
    skill = args.get("skill") or ""

    source_execution_id = args.get("source_execution_id")
    conversation_id = context.get("conversation_id") if context else None

    # --- GUARD 1: no execution_id AND no usable payload → BLOCK ---
    if not source_execution_id:
        has_data = bool(payload)
        if not has_data:
            raise ToolExecutionError(
                "create_artifact called with no source_execution_id and no "
                "structured payload. For re-exports, pass "
                "source_execution_id=evt_xxx. For new analyses, populate "
                "the payload with at least one of: chart, kpis, data_table, "
                "slides, html_content, summary."
            )

    # --- GUARD 2: resolve execution identity ---
    # When source_execution_id is present, resolve the cached DataExecution
    # row and reject missing / expired / cross-session executions. When
    # absent, fall back to the session's last cached execution (fail-open:
    # any lookup failure simply leaves ``execution`` as None).
    execution = None
    if source_execution_id:
        execution = DataExecutionService.get_by_id(db, source_execution_id)
        if not execution or execution.is_expired():
            raise ToolExecutionError(
                f"source_execution_id {source_execution_id!r} not found or "
                f"expired (TTL: 7 days)."
            )
        if execution.session_id != conversation_id:
            raise ToolExecutionError(
                f"source_execution_id {source_execution_id!r} belongs to a "
                f"different session."
            )
    else:
        # Fail-open: session-state lookup is best-effort; any error here
        # (e.g. a non-ORM db in tests) must not break payload-only flows.
        # isinstance() also rejects MagicMock-ish "rows" so a fake db can
        # never fabricate an execution and pollute the payload.
        try:
            state = SessionStateService.get_last(db, session_id=conversation_id)
        except Exception:
            state = None
        if isinstance(state, SessionState) and state.last_execution_id:
            execution = DataExecutionService.get_by_id(db, state.last_execution_id)

    # --- Enrich payload from the cached DataExecution ---
    # GUARD 2 resolved ``execution``; map its cached result into the
    # ReportCard payload, letting the LLM's explicit payload fields win.
    if execution:
        cached_payload = _payload_from_execution(execution)
        payload = {**cached_payload, **payload}
        args["payload"] = payload
        rows = (execution.result or {}).get("rows") or []
        if len(rows) == 0:
            payload.setdefault("sections", []).append({
                "title": "Data Quality Note",
                "content": (
                    f"The cached data tool ({execution.tool_name}) returned 0 "
                    f"rows. The document below reflects that empty result."
                ),
                "type": "narrative",
            })

    if artifact_type not in ("docx", "pdf", "pptx", "html"):
        return {
            "success": False,
            "error": f"Unsupported artifact type '{artifact_type}'. "
                     f"Must be one of: docx, pdf, pptx, html.",
        }

    if not title:
        return {"success": False, "error": "title is required"}

    if not payload or not isinstance(payload, dict):
        return {"success": False, "error": "payload must be a non-empty object"}

    agent_app_id = context.get("agent_app_id") if context else None
    # Execution identity links the artifact to the run that fetched its data,
    # so a deck can be grounded in that execution's REAL query rows.
    execution_id = context.get("execution_id") if context else None
    org_id = (context or {}).get("org_id", "default-org")
    app_id = (context or {}).get("app_id", "default-app")

    # --- Enrich sparse payload from a sibling rich HTML report ---
    # The agent often calls create_artifact right after
    # ``finalize_into_artifact`` to convert the rich HTML report
    # card into a downloadable file.  In that case the rich
    # ReportCardPayload lives on the ``html_report`` artifact, but
    # this create_artifact call's payload is usually just
    # ``{html_path, filename}`` or similarly sparse — and the
    # resulting docx/pptx is just a title + bare data table.  Look
    # up the matching ``html_report`` in the same conversation
    # and fill in any blank field so the docx renders the same
    # rich content as the HTML report the user just saw.
    if conversation_id and artifact_type in ("docx", "pptx", "pdf"):
        payload = _enrich_payload_from_sibling_html_report(
            payload=payload,
            db=db,
            conversation_id=conversation_id,
            title=title,
        )
        args["payload"] = payload

    # Phase 1B: persist the DeckPlan into source_json so PHASE 2 edit tools can
    # round-trip through DeckPlan.model_validate.  The orchestrator ships the
    # plan dict in args["deck_plan"] for pptx.
    if artifact_type == "pptx":
        deck_plan = args.get("deck_plan")
        if isinstance(deck_plan, dict):
            payload = dict(payload)
            payload["deck_plan"] = deck_plan
        elif hasattr(deck_plan, "model_dump"):
            payload = dict(payload)
            payload["deck_plan"] = deck_plan.model_dump(mode="json")

    used_pipeline = False
    try:
        from app.services.artifacts.artifact_service import ArtifactService
        from app.services.artifacts.preview_builder import convert_to_preview

        service = ArtifactService(db)

        # 1. Create the artifact
        artifact = service.create_artifact(
            artifact_type=artifact_type,
            title=title,
            description=description,
            conversation_id=conversation_id,
            execution_id=execution_id,
            created_by_agent_id=agent_app_id,
            org_id=org_id,
            app_id=app_id,
        )

        # 2. Create an initial version
        version = service.create_version(
            artifact_id=artifact.id,
            changelog=f"Generated via create_artifact ({artifact_type})",
            source_json=payload,
            produced_by_skill=skill or None,
        )
        if not version:
            return {"success": False, "error": "Failed to create artifact version"}

        # 3. Render the file bytes. Long sync renders MUST be offloaded to a
        #    worker thread (_run_blocking) so the SSE heartbeat keeps flowing;
        #    a stalled render fails over instead of dropping the stream.
        ext = _TYPE_EXT[artifact_type]
        mime = _TYPE_MIME[artifact_type]
        file_name = _safe_filename(title, ext)

        try:
            if artifact_type == "html":
                html_content = payload.get("html_content", "")
                if isinstance(html_content, dict):
                    html_content = str(html_content)
                if not html_content:
                    return {"success": False, "error": "html_content is required for html type"}
                data = html_content.encode("utf-8")
            elif artifact_type == "pptx":
                rcp = _payload_to_reportcard(payload, title)
                if (
                    settings.PPT_DECK_PLANNER_ENABLED
                    and settings.PPT_CREATE_ARTIFACT_PIPELINE_ENABLED
                ):
                    # Professional pipeline (planner → router → layout engine →
                    # audit/repair/polish): render through the SAME
                    # ExportService.render_pptx_deck the download path uses, so
                    # the in-chat deck and the downloaded file are consistent.
                    # Grounding + brand theming + personalization apply here.
                    from app.services.artifacts.exporters.service import ExportService

                    exporter = ExportService(db)
                    user_message_arg = str(args.get("user_message", ""))
                    ctx, brand_fp = _build_deck_export_context(
                        exporter, artifact,
                        conversation_id=conversation_id,
                        user_message=user_message_arg,
                        org_id=org_id,
                        app_id=app_id,
                    )
                    rows = _collect_rows_for_tool(
                        db, rcp,
                        artifact=artifact,
                        conversation_id=conversation_id,
                        execution_id=execution_id,
                        user_message=user_message_arg,
                    )
                    data, mime, ext = await _run_blocking(
                        exporter.render_pptx_deck,
                        rcp, ctx, rows,
                        user_message=user_message_arg,
                        artifact=artifact,
                        user_id=user_id,
                    )
                    file_name = _safe_filename(title, ext)
                    used_pipeline = True
                    # Byte-consistency: register the exact bytes as the cached
                    # format_export blob so GET /download?format=pptx returns
                    # THIS deck (not a fresh LLM re-render with different rows).
                    try:
                        exporter._attach_format_blob(
                            artifact=artifact, version=version,
                            format="pptx", file_name=file_name,
                            mime_type=mime, data=data,
                            theme_fingerprint=brand_fp,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to register deck format_export blob for "
                            "artifact %s: %s", artifact.id, exc,
                        )
                else:
                    from app.services.artifacts.exporters.pptx_export import render as _render_pptx
                    data, mime, ext = await _run_blocking(_render_pptx, rcp)
                    file_name = _safe_filename(title, ext)
            elif artifact_type == "docx":
                from app.services.artifacts.exporters.docx_export import render as _render_docx
                rcp = _payload_to_reportcard(payload, title)
                data, mime, ext = await _run_blocking(_render_docx, rcp)
                file_name = _safe_filename(title, ext)
            elif artifact_type == "pdf":
                from app.services.artifacts.exporters.pdf_export import render as _render_pdf
                rcp = _payload_to_reportcard(payload, title)
                data, mime, ext = await _run_blocking(_render_pdf, rcp)
                file_name = _safe_filename(title, ext)
            else:
                return {"success": False, "error": f"Unknown artifact type: {artifact_type}"}
        except asyncio.TimeoutError:
            logger.error(
                "artifact render timed out after %ss: %s",
                ARTIFACT_RENDER_TIMEOUT_S, file_name,
            )
            return {
                "success": False,
                "error": (
                    f"Rendering {file_name} timed out after "
                    f"{ARTIFACT_RENDER_TIMEOUT_S}s; the file format is too "
                    "complex - try a simpler artifact."
                ),
            }

        # 4. Store the original blob
        service.store_blob(
            version_id=version.id,
            blob_type="original",
            file_name=file_name,
            mime_type=mime,
            data=data,
        )

        # 5. Generate and store preview (offloaded too - preview conversion is
        #    also multi-second and must not block the heartbeat).
        has_preview = False
        preview_result = await _run_blocking(
            convert_to_preview, data, file_name, artifact_type,
        )
        if preview_result:
            preview_data, preview_name, preview_mime = preview_result
            service.store_blob(
                version_id=version.id,
                blob_type="preview",
                file_name=preview_name,
                mime_type=preview_mime,
                data=preview_data,
            )
            has_preview = True
            # Try thumbnail for PDF previews
            if preview_mime == "application/pdf":
                try:
                    from app.services.artifacts.preview_builder import generate_thumbnail
                    thumb = await _run_blocking(generate_thumbnail, preview_data)
                    if thumb:
                        service.store_blob(
                            version_id=version.id,
                            blob_type="thumbnail",
                            file_name=f"{file_name.rsplit('.', 1)[0]}.png",
                            mime_type="image/png",
                            data=thumb,
                        )
                except Exception:
                    pass

        # 6. Mark version as built
        service.mark_version_built(version.id)

        # Update artifact status
        service.update_status(artifact.id, "preview_ready")

        # When the professional pipeline was used, persist the enriched payload
        # + owner so any future re-render reconstructs the same content and can
        # re-personalize (the download route has no auth, so it reads
        # owner_user_id off the artifact's metadata).
        if used_pipeline:
            try:
                meta = dict(artifact.metadata_json or {})
                meta["report_card_payload"] = payload
                if user_id:
                    meta["owner_user_id"] = user_id
                artifact.metadata_json = meta
                db.commit()
            except Exception as exc:
                logger.warning(
                    "Failed to persist deck metadata for artifact %s: %s",
                    artifact.id, exc,
                )

        # 6b. For file-format artifacts (docx / pptx / xlsx), auto-create
        #     a rich sidecar HTML preview so the dedup layer can pair
        #     them and the in-chat preview pane shows the rich HTML
        #     instead of the sparse mammoth conversion.
        preview_artifact_id: Optional[str] = None
        if artifact_type in ("docx", "pptx", "xlsx"):
            try:
                rcp_for_sidecar = _payload_to_reportcard(payload, title)
                preview_artifact_id = await _run_blocking(
                    _create_sidecar_preview,
                    service,
                    artifact,
                    rcp_for_sidecar,
                    user_message=str(args.get("user_message", "")),
                    conversation_id=conversation_id,
                    agent_app_id=agent_app_id,
                )
            except Exception as exc:
                logger.warning(
                    "Sidecar preview raised for artifact %s: %s",
                    artifact.id, exc,
                )

        # 7. Build response URLs (relative, frontend prepends the API base)
        preview_url = f"/api/artifacts/{artifact.id}/preview" if has_preview else None
        download_url = f"/api/artifacts/{artifact.id}/download"

        result = {
            "success": True,
            "artifact_id": artifact.id,
            "version_id": version.id,
            "version_number": version.version_number,
            "file_url": download_url,
            "preview_url": preview_url,
            "file_name": file_name,
            "mime_type": mime,
            "title": title,
            "type": artifact_type,
            "file_size": len(data),
            "has_preview": has_preview,
            "preview_artifact_id": preview_artifact_id,
            "payload": payload,
        }

        logger.info(
            "create_artifact tool produced %s (%d bytes) — artifact=%s, preview=%s",
            file_name, len(data), artifact.id, has_preview,
        )

        # Phase 5 — usage instrumentation (fire-and-forget, pptx only).
        if artifact_type == "pptx":
            org_id = (context or {}).get("org_id", "default-org")
            app_id = (context or {}).get("app_id", "default-app")
            slide_count = None
            try:
                slide_count = len(payload.get("deck_plan", {}).get("slides", [])) or None
            except Exception:
                slide_count = None
            log_deck_event_fire_and_forget(
                None, "deck_generated", artifact_id=artifact.id, user_id=user_id,
                metadata={
                    "title": title,
                    "slide_count": slide_count,
                    "has_preview": has_preview,
                },
                org_id=org_id, app_id=app_id,
            )
        return result

    except Exception as exc:
        logger.exception("create_artifact failed for type=%s title=%s", artifact_type, title)
        # If we got far enough to create the version row, transition it
        # (and its parent artifact) to "failed" so the UI doesn't show an
        # indefinite "building" spinner. mark_version_failed is a no-op if
        # the version was never created (failures in steps 1-2).
        try:
            if "version" in locals() and version is not None and "service" in locals():
                service.mark_version_failed(version.id, f"Artifact creation failed: {exc}")
        except Exception:
            logger.exception("Failed to mark version as failed during cleanup")
        return {"success": False, "error": f"Artifact creation failed: {exc}"}


def _payload_to_reportcard(payload: dict, title: str) -> ReportCardPayload:
    """Convert a dict payload (from the LLM) into a ReportCardPayload instance."""
    from app.services.synexia.contracts import ActionSpec, ChartSpec, InsightSpec, KPISpec, SectionSpec

    def _chart_data(p: dict) -> list[dict]:
        cd = p.get("chart", {}).get("data") if isinstance(p.get("chart"), dict) else None
        if isinstance(cd, list):
            return cd
        return []

    chart = None
    chart_data = _chart_data(payload)
    if chart_data:
        chart_dict = payload.get("chart", {}) if isinstance(payload.get("chart"), dict) else {}
        x_key = chart_dict.get("x_key")
        y_keys = chart_dict.get("y_keys")

        # Fall back: infer x_key / y_keys from the first row of chart_data
        # when the agent omits them. First dict key -> x-axis, remaining
        # keys -> y-axis series. If we can't infer a usable y axis, drop
        # the chart (the artifact still renders with KPIs / insights).
        if (not x_key or not y_keys) and isinstance(chart_data[0], dict):
            first_keys = list(chart_data[0].keys())
            if not x_key and first_keys:
                x_key = first_keys[0]
            if not y_keys and len(first_keys) >= 2:
                y_keys = first_keys[1:]

        if x_key and y_keys:
            chart = ChartSpec(
                title=chart_dict.get("title") or "Chart",
                type=chart_dict.get("type") or "bar",
                x_key=x_key,
                y_keys=list(y_keys),
                data=chart_data,
            )

    def _to_insights(items) -> list[InsightSpec]:
        out: list[InsightSpec] = []
        for i in (items or []):
            if isinstance(i, dict):
                out.append(InsightSpec(
                    text=i.get("text", ""),
                    icon=(i.get("icon") or "lightbulb"),
                ))
            elif i is not None:
                out.append(InsightSpec(text=str(i), icon="lightbulb"))
        return out

    return ReportCardPayload(
        title=payload.get("title") or title,
        source=payload.get("source") or "",
        generated_at=datetime.now(timezone.utc).isoformat(),
        summary=payload.get("summary") or "",
        kpis=[
            KPISpec(
                label=k.get("label", ""),
                value=str(k.get("value", "")),
                delta=k.get("delta"),
                caption=k.get("caption"),
            )
            for k in (payload.get("kpis") or [])
        ],
        chart=chart,
        insights=_to_insights(payload.get("insights")),
        next_step=payload.get("next_step"),
        user_signal=payload.get("user_signal") or "export",
        # v2 Claude-style fields
        methodology=payload.get("methodology") or "",
        key_findings=_to_insights(payload.get("key_findings")),
        recommendations=_to_insights(payload.get("recommendations")),
        sections=[
            SectionSpec(
                title=s.get("title", ""),
                content=s.get("content", "") or "",
                bullets=list(s.get("bullets") or []),
                type=s.get("type", "narrative") or "narrative",
            )
            for s in (payload.get("sections") or [])
        ],
        sql=payload.get("sql") or "",
        # Fields not previously propagated — now passed through so
        # payload assemblers (e.g. ForecastPayloadAssembler) can
        # supply their own warnings and actions.
        warnings=payload.get("warnings") or [],
        actions=[
            ActionSpec(
                label=a.get("label", ""),
                prompt=a.get("prompt", ""),
            )
            for a in (payload.get("actions") or [])
        ],
    )


# --- Tool schema ---

CREATE_ARTIFACT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_artifact",
        "description": (
            "Produce a downloadable document (docx, pdf, pptx, html) and "
            "surface it inline in the chat. "
            "PREFERRED for re-exports: pass `source_execution_id` (the "
            "evt_xxx id from a previous data tool result) instead of "
            "`payload` — the platform will re-build the document from "
            "the cached structured data, so you do not need to "
            "re-run the data tool. "
            "If `source_execution_id` is omitted, `payload` is used "
            "(legacy path). For 'html' type, pass {html_content: string}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["docx", "pdf", "pptx", "html"]},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "skill": {"type": "string"},
                "source_execution_id": {
                    "type": "string",
                    "description": (
                        "OPTIONAL. The evt_xxx id of a cached data tool "
                        "execution in this session. When provided, the "
                        "document is built from the cached structured "
                        "result — no re-run needed."
                    ),
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "For docx/pdf/pptx: a ReportCard-shaped object with "
                        "optional fields: title, summary, source, kpis (array "
                        "of {label, value, delta?, caption?}), chart ({title, "
                        "type, data}), insights (array of {text, icon?}), "
                        "next_step, slides (for pptx only — array of {title, "
                        "bullets?}), user_signal. For html: {html_content: string}."
                    ),
                    "properties": {
                        "html_content": {"type": "string"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "source": {"type": "string"},
                        "kpis": {"type": "array"},
                        "chart": {"type": "object"},
                        "insights": {"type": "array"},
                        "slides": {
                            "type": "array",
                            "description": "For pptx: list of slide definitions {title, bullets?}.",
                        },
                        "next_step": {"type": "string"},
                        "user_signal": {"type": "string"},
                    },
                },
            },
            "required": ["type", "title"],
        },
    },
}

# Register the tool
registry.register(
    name="create_artifact",
    schema=CREATE_ARTIFACT_SCHEMA,
    handler=_create_artifact_tool,
    category="artifacts",
    toolset="artifacts",
    description="Produce a downloadable document (docx, pdf, pptx, html) and surface it inline in the chat.",
    emoji="📄",
    max_result_size_chars=4_000,
)


# ── edit_artifact ──────────────────────────────────────────────────────


async def _edit_artifact_tool(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    """Handler for the edit_artifact LLM tool.

    Refine an existing artifact via natural-language instructions.
    The LLM interprets the instruction against the current source_json
    payload, produces a modified payload, and the tool re-renders the
    file with a new version.

    Args:
        args: Must contain ``artifact_id`` (str/int) and ``instructions``
              (str).  Optional: ``title``, ``format``, ``payload``
              (partial overrides that bypass the LLM interpretation step).
    """
    if db is None:
        return {"success": False, "error": "Database session is required"}

    artifact_id = args.get("artifact_id")
    instructions = (args.get("instructions") or "").strip()

    if not artifact_id:
        return {"success": False, "error": "artifact_id is required"}
    if not instructions and not args.get("payload"):
        return {"success": False, "error": "instructions or payload overrides are required"}

    # Normalise artifact_id (LLMs sometimes pass an int)
    try:
        artifact_id = str(artifact_id)
    except (ValueError, TypeError):
        return {"success": False, "error": f"Invalid artifact_id: {artifact_id}"}

    conversation_id = context.get("conversation_id") if context else None
    agent_app_id = context.get("agent_app_id") if context else None

    try:
        from app.services.artifacts.artifact_service import ArtifactService
        from app.services.artifacts.preview_builder import convert_to_preview

        service = ArtifactService(db)

        # 1. Look up the existing artifact
        artifact = service.get_artifact(artifact_id)
        if artifact is None:
            return {"success": False, "error": f"Artifact {artifact_id} not found"}

        # 2. Get the current version's source_json payload
        prev_version = service.get_current_version(artifact_id)
        if prev_version is None:
            return {"success": False, "error": f"No version found for artifact {artifact_id}"}

        current_payload: dict = (prev_version.source_json or {})
        if isinstance(current_payload, str):
            import json as _json
            try:
                current_payload = _json.loads(current_payload)
            except Exception:
                current_payload = {"_raw": current_payload}
        if not isinstance(current_payload, dict):
            current_payload = {}
        # Normalise legacy / wrapper shapes
        current_payload = _unwrap_payload(current_payload)

        artifact_type = artifact.artifact_type or "pptx"

        # 3. Produce the modified payload
        modified_payload: dict
        direct_overrides = args.get("payload")
        if isinstance(direct_overrides, dict) and direct_overrides and not instructions:
            # Purely declarative override — merge directly (no LLM call).
            modified_payload = dict(current_payload)
            _deep_merge(modified_payload, direct_overrides)
        else:
            # Natural-language instruction — use LLM to interpret the edit.
            modified_payload = await _llm_edit_payload(
                artifact_type=artifact_type,
                current_payload=current_payload,
                instructions=instructions,
                direct_overrides=direct_overrides if isinstance(direct_overrides, dict) else None,
            )

        # 4. Create a new version with the modified payload
        new_title = (args.get("title") or current_payload.get("title") or artifact.title or "Untitled").strip()
        # Also update title in the payload so the renderer picks it up
        modified_payload["title"] = new_title

        version = service.create_version(
            artifact_id=artifact.id,
            changelog=instructions or "Payload overrides applied",
            source_json=modified_payload,
            produced_by_skill=prev_version.produced_by_skill or None,
        )
        if not version:
            return {"success": False, "error": "Failed to create artifact version"}

        # 5. Re-render the file bytes. Long sync renders MUST be offloaded to
        #    a worker thread (_run_blocking) so the SSE heartbeat keeps
        #    flowing; a stalled render fails over instead of dropping the stream.
        ext = _TYPE_EXT.get(artifact_type, ".pptx")
        mime = _TYPE_MIME.get(artifact_type, _TYPE_MIME["pptx"])
        file_name = _safe_filename(new_title, ext)

        try:
            if artifact_type == "html":
                html_content = modified_payload.get("html_content", "")
                if isinstance(html_content, dict):
                    html_content = str(html_content)
                if not html_content:
                    # Keep the old html_content if the edit didn't change it
                    html_content = current_payload.get("html_content", "")
                    if isinstance(html_content, dict):
                        html_content = str(html_content)
                if not html_content:
                    html_content = "<html><body><h1>{}</h1></body></html>".format(
                        _esc(new_title)
                    )
                data = html_content.encode("utf-8")
            elif artifact_type == "pptx":
                from app.services.artifacts.exporters.pptx_export import render as _render_pptx
                rcp = _payload_to_reportcard(modified_payload, new_title)
                data, mime, ext = await _run_blocking(_render_pptx, rcp)
                file_name = _safe_filename(new_title, ext)
            elif artifact_type == "docx":
                from app.services.artifacts.exporters.docx_export import render as _render_docx
                rcp = _payload_to_reportcard(modified_payload, new_title)
                data, mime, ext = await _run_blocking(_render_docx, rcp)
                file_name = _safe_filename(new_title, ext)
            elif artifact_type == "pdf":
                from app.services.artifacts.exporters.pdf_export import render as _render_pdf
                rcp = _payload_to_reportcard(modified_payload, new_title)
                data, mime, ext = await _run_blocking(_render_pdf, rcp)
                file_name = _safe_filename(new_title, ext)
            else:
                return {"success": False, "error": f"Unknown artifact type: {artifact_type}"}
        except asyncio.TimeoutError:
            logger.error(
                "artifact re-render timed out after %ss: %s",
                ARTIFACT_RENDER_TIMEOUT_S, file_name,
            )
            return {
                "success": False,
                "error": (
                    f"Rendering {file_name} timed out after "
                    f"{ARTIFACT_RENDER_TIMEOUT_S}s; the file format is too "
                    "complex - try a simpler edit."
                ),
            }

        # 6. Store the new blob
        service.store_blob(
            version_id=version.id,
            blob_type="original",
            file_name=file_name,
            mime_type=mime,
            data=data,
        )

        # 7. Generate and store preview (offloaded too - preview conversion is
        #    also multi-second and must not block the heartbeat).
        has_preview = False
        preview_result = await _run_blocking(
            convert_to_preview, data, file_name, artifact_type,
        )
        if preview_result:
            preview_data, preview_name, preview_mime = preview_result
            service.store_blob(
                version_id=version.id,
                blob_type="preview",
                file_name=preview_name,
                mime_type=preview_mime,
                data=preview_data,
            )
            has_preview = True
            # Try thumbnail for PDF previews
            if preview_mime == "application/pdf":
                try:
                    from app.services.artifacts.preview_builder import generate_thumbnail
                    thumb = await _run_blocking(generate_thumbnail, preview_data)
                    if thumb:
                        service.store_blob(
                            version_id=version.id,
                            blob_type="thumbnail",
                            file_name=f"{file_name.rsplit('.', 1)[0]}.png",
                            mime_type="image/png",
                            data=thumb,
                        )
                except Exception:
                    pass

        # 8. Mark version as built
        service.mark_version_built(version.id)

        # Update artifact title if changed
        if new_title and new_title != artifact.title:
            artifact.title = new_title
            db.add(artifact)
        service.update_status(artifact.id, "preview_ready")

        # 9. Build response
        preview_url = f"/api/artifacts/{artifact.id}/preview" if has_preview else None
        download_url = f"/api/artifacts/{artifact.id}/download"

        result = {
            "success": True,
            "action": "edited",
            "artifact_id": artifact.id,
            "version_id": version.id,
            "version_number": version.version_number,
            "previous_version_number": prev_version.version_number,
            "file_url": download_url,
            "preview_url": preview_url,
            "file_name": file_name,
            "mime_type": mime,
            "title": new_title,
            "type": artifact_type,
            "file_size": len(data),
            "has_preview": has_preview,
            # Include a compact change summary for the agent to use in its response
            "change_summary": _summarise_changes(current_payload, modified_payload, instructions),
        }

        logger.info(
            "edit_artifact: artifact=%s v%d→v%d, instructions=%r",
            artifact.id, prev_version.version_number, version.version_number,
            instructions[:120] if instructions else "(direct overrides)",
        )
        return result

    except Exception as exc:
        logger.exception("edit_artifact failed for artifact_id=%s", artifact_id)
        try:
            if "version" in locals() and version is not None and "service" in locals():
                service.mark_version_failed(version.id, f"Artifact edit failed: {exc}")
        except Exception:
            pass
        return {"success": False, "error": f"Artifact edit failed: {exc}"}


# --- Helpers for edit_artifact ---


def _unwrap_payload(payload: dict) -> dict:
    """Unwrap common nesting patterns in source_json so the edit LLM sees
    a flat report-card dict rather than a wrapper.

    Patterns handled:
      - ``{rcp: {...}}`` or ``{report_card_payload: {...}}``
      - ``{"source_json": "{\\"title\\":...}"}``  (stringified inner json)
      - Bare payload with top-level ``type`` / ``title`` / ``kpis`` / etc.
    """
    import json as _json

    if isinstance(payload, str):
        try:
            payload = _json.loads(payload)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}

    # Unwrap common wrapper keys
    for wrapper_key in ("rcp", "report_card_payload", "report_card"):
        inner = payload.get(wrapper_key)
        if isinstance(inner, dict) and inner:
            return _unwrap_payload(inner)

    # If the top-level payload has a 'payload' key that is a dict, unwrap it
    inner_payload = payload.get("payload")
    if isinstance(inner_payload, dict) and inner_payload:
        # Check if there's meaningful content beyond just a wrapper
        inner_keys = set(inner_payload.keys())
        wrapper_only_keys = {"rcp", "report_card_payload", "payload"}
        if inner_keys - wrapper_only_keys:
            return _unwrap_payload(inner_payload)

    return payload


def _deep_merge(base: dict, overrides: dict) -> None:
    """Merge ``overrides`` into ``base`` in place, recursively for nested dicts."""
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        elif isinstance(v, list) and isinstance(base.get(k), list):
            # For lists (slides, kpis, insights), replace item-by-item
            # by index when the override supplies a list; otherwise
            # just replace the whole list.
            if v and isinstance(v[0], dict) and all(isinstance(bi, dict) for bi in (base[k] or [])):
                base_list = list(base[k] or [])
                for i, item in enumerate(v):
                    if i < len(base_list):
                        _deep_merge(base_list[i], item)
                    else:
                        base_list.append(item)
                base[k] = base_list
            else:
                base[k] = v
        else:
            base[k] = v


async def _llm_edit_payload(
    *,
    artifact_type: str,
    current_payload: dict,
    instructions: str,
    direct_overrides: Optional[dict] = None,
) -> dict:
    """Use the LLM to interpret an edit instruction and produce a modified payload.

    Args:
        artifact_type: One of docx / pptx / pdf / html.
        current_payload: The current source_json (unwrapped) as a dict.
        instructions: Natural-language edit instruction from the user.
        direct_overrides: Optional partial overrides to apply after the LLM
            interpretation (applied as safety-net key-value replacements).

    Returns:
        The modified payload dict as returned by the LLM (best-effort;
        falls back to the original payload on failure).
    """
    from app.services.llm_service import call_llm
    import json as _json

    # Serialize current payload as pretty JSON for the LLM
    current_json = _json.dumps(current_payload, ensure_ascii=False, indent=2, default=str)

    # Build a compact prompt that asks the LLM to edit the payload
    system_prompt = (
        f"You are a document payload editor. The user wants to modify a "
        f"{artifact_type.upper()} document. Below is the current structured "
        f"payload (ReportCard JSON). Apply the user's edit instruction and "
        f"return the FULL modified JSON object."
        f"\n\nRules:\n"
        f"- Preserve ALL existing fields unless the instruction explicitly "
        f"removes or replaces them.\n"
        f"- Keep the JSON structure identical (same keys, same nesting).\n"
        f"- For pptx: edit the 'slides' array when the instruction mentions "
        f"slides, titles, or bullet points.\n"
        f"- For docx/pdf: edit 'summary', 'methodology', 'kpis', "
        f"'key_findings', 'recommendations', 'sections', 'chart' as needed.\n"
        f"- If the instruction asks to ADD something, add it to the "
        f"appropriate field.\n"
        f"- If the instruction asks to CHANGE something, update the field "
        f"in place.\n"
        f"- If the instruction asks to make something 'more professional', "
        f"'better', or similar qualitative requests, improve the language "
        f"in the text fields (summary, key_findings, recommendations, "
        f"slide content) while preserving factual content.\n"
        f"- Return ONLY the JSON object (no markdown fences, no explanation).\n"
        f"- If you truly cannot interpret the instruction, return the "
        f"original JSON unchanged.\n\n"
        f"=== Current Payload ({artifact_type}) ===\n"
        f"{current_json}\n\n"
        f"=== Edit Instruction ===\n"
        f"{instructions}"
    )

    overrides_block = ""
    if direct_overrides and isinstance(direct_overrides, dict):
        overrides_block = (
            "\n\n=== Direct Overrides (apply last, highest priority) ===\n"
            + _json.dumps(direct_overrides, ensure_ascii=False, indent=2, default=str)
        )
        system_prompt += overrides_block

    try:
        result = call_llm(
            prompt=system_prompt,
            messages=[],
            temperature=0.2,  # Low temperature for precise edits
        )
        response_text = (result.get("response") or "").strip()

        # Strip markdown fence if present
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            # Remove opening fence
            if lines[0].startswith("```"):
                lines = lines[1:]
            # Remove closing fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            response_text = "\n".join(lines).strip()

        modified = _json.loads(response_text)
        if isinstance(modified, dict) and modified:
            return modified
        else:
            logger.warning("edit_artifact LLM returned non-dict, falling back to original")
            return dict(current_payload)
    except Exception as e:
        logger.warning("edit_artifact LLM call failed (non-fatal): %s", e)
        # Fall back: merge direct overrides if provided, else return original
        if direct_overrides and isinstance(direct_overrides, dict):
            fallback = dict(current_payload)
            _deep_merge(fallback, direct_overrides)
            return fallback
        return dict(current_payload)


def _summarise_changes(original: dict, modified: dict, instructions: str) -> str:
    """Produce a human-readable change summary for the agent's response."""
    if not instructions:
        return "Payload overrides applied."
    # Compact: detect which top-level keys changed
    changed_keys = []
    all_keys = set(list(original.keys()) + list(modified.keys()))
    for k in sorted(all_keys):
        if k in ("generated_at", "rcp", "report_card_payload"):
            continue
        ov = original.get(k)
        mv = modified.get(k)
        if ov != mv:
            if isinstance(ov, list) and isinstance(mv, list) and len(mv) != len(ov):
                changed_keys.append(f"{k} ({len(ov)}→{len(mv)} items)")
            else:
                changed_keys.append(k)
    if changed_keys:
        return f"Edited: {', '.join(changed_keys[:5])}"
    return f"Applied: {instructions[:80]}"


# --- Schema ---

EDIT_ARTIFACT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "edit_artifact",
        "description": (
            "Refine an existing artifact (document, presentation, report) "
            "using natural-language instructions. Use this when the user "
            "wants to modify content they already generated — e.g. 'make "
            "the title bolder', 'add a summary slide', 'change the KPI "
            "headline to Q3 results', 'make it more professional'. "
            "The tool loads the current payload, applies your edit via the "
            "LLM, re-renders the file, and returns the updated version. "
            "Always call this instead of create_artifact when refining an "
            "existing document."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "The ID of the existing artifact to edit.",
                },
                "instructions": {
                    "type": "string",
                    "description": (
                        "Natural-language description of what to change. "
                        "Be specific: mention which slide, which KPI, or "
                        "which section to modify. Qualitative instructions "
                        "('make it more professional', 'improve the flow') "
                        "are also supported — the LLM will interpret them."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Optional new title for the artifact (changes the file name too).",
                },
                "format": {
                    "type": "string",
                    "enum": ["docx", "pdf", "pptx", "html"],
                    "description": "Optional target format (converts the artifact if different from current).",
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "Optional direct key-value overrides (shortcut for "
                        "simple changes like renaming a KPI label). When "
                        "instructions is also provided, overrides merge in "
                        "after the LLM interpretation."
                    ),
                },
            },
            "required": ["artifact_id", "instructions"],
        },
    },
}

# Register the tool
registry.register(
    name="edit_artifact",
    schema=EDIT_ARTIFACT_SCHEMA,
    handler=_edit_artifact_tool,
    category="artifacts",
    toolset="artifacts",
    description="Refine an existing document (pptx, docx, pdf, html) with natural-language instructions.",
    emoji="✏️",
    max_result_size_chars=3_000,
)
