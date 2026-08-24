"""FINALIZE stage — turn a FinalizeResult into a versioned Artifact + in-chat payload.

This is the "two surfaces" implementation:

1. ALWAYS write an `Artifact` row (kind=`html_report`) so the
   download/export flow has something to point at.  The Artifact
   record is the canonical, versioned, cited evidence of the report.
2. ALWAYS attach a `ReportCardPayload` to the assistant message so the
   in-chat ReportCard.jsx component can render title + KPIs + chart +
   insights + Export bar.
3. The frontend decides the primary surface from `user_signal`:
   `default` → in-chat card; `export` → downloadable artifact + card.

We do NOT call the artifact-export skills (PDF/PPTX/XLSX) here — that
is task 6 (`artifact-export-skills`).  This module produces the
`html_report` row and registers the payload so the download skills can
attach their file blobs to the same `payload.formats` dict later.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Tuple
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.artifact import Artifact, ArtifactVersion, ArtifactBlob, MessageArtifact
from app.models.execution import ObservationRecord as DBObservationRecord
from app.services.synexia.contracts import FinalizeResult, ReportCardPayload, KPISpec, InsightSpec, SectionSpec
from app.services.artifacts.exporters.service import ExportService

logger = logging.getLogger(__name__)


def build_no_data_payload(
    *,
    user_message: str,
    source: Optional[str],
    sql: Optional[str],
) -> ReportCardPayload:
    """Deterministic ReportCardPayload for a successful query with 0 rows.

    Used when the user asked for a downloadable file but ``ask_data_agent``
    returned no rows: instead of letting the whole report/artifact chain
    die, we finalize a graceful "no data" report so the user still gets
    the requested file with a clear narrative (never a silent dead end).

    The single KPI tile doubles as the one data row for the file-export
    sandbox path (which requires a non-empty data array).
    """
    title_src = (user_message or "").strip()
    title = title_src[:80] if title_src else "Data report"
    summary = (
        "The query ran successfully but returned no data, so there are no "
        "results to chart or tabulate. The report below records the query "
        "context so the file is still usable as a template."
    )
    insights = [
        InsightSpec(icon="info", text="The query executed successfully but returned 0 rows."),
    ]
    if sql:
        insights.append(
            InsightSpec(icon="database", text=f"Executed SQL: {sql[:300]}")
        )
    insights.append(
        InsightSpec(
            icon="lightbulb",
            text="Try broadening the filters, date range, or search terms and run the query again.",
        )
    )
    return ReportCardPayload(
        title=title,
        source=source or "",
        generated_at=datetime.now(timezone.utc).isoformat(),
        summary=summary,
        kpis=[KPISpec(label="Rows returned", value="0")],
        chart=None,
        insights=insights,
        next_step="Broaden the query filters and regenerate the report.",
        user_signal="default",
        # Claude-style fields — even the no-data case gets a useful
        # methodology section + recommendations so the docx / sidecar
        # preview show a coherent business report, not a near-empty file.
        methodology=(
            f"Executed a SQL query against `{source or 'unknown'}` that returned "
            "0 rows. The query ran to completion but no records matched the "
            "filter criteria."
        ),
        key_findings=[
            InsightSpec(
                icon="info",
                text="The query was syntactically valid and ran to completion, but no records matched the current filter.",
            ),
            InsightSpec(
                icon="alert-triangle",
                text=(
                    "This usually means the dataset exists but the current "
                    "WHERE clause is too narrow (date range, region, status, …)."
                ),
            ),
        ],
        recommendations=[
            InsightSpec(
                icon="lightbulb",
                text="Remove or relax the most restrictive filter and re-run the report.",
            ),
            InsightSpec(
                icon="database",
                text=(
                    "If the data is genuinely missing, connect a different "
                    "database or import a fresh dataset before re-running."
                ),
            ),
        ],
        sections=[
            SectionSpec(
                title="What was searched",
                content=(
                    "This report documents a query that returned no rows so "
                    "you can see exactly what was tried before broadening "
                    "the search."
                ),
            ),
        ],
        sql=sql or "",
    )


def _render_html_report(
    payload: ReportCardPayload,
    *,
    user_message: str,
    source: str,
    sql: str | None,
    theme=None,
) -> str:
    """Render a minimal self-contained HTML report for the Artifact blob.

    This is a v1 render — a real implementation would call the
    `[skill:pdf]` / `[skill:html]` skill to generate a richer output.
    Kept simple + standalone (no external CDN, no JS) so it always
    renders even if the artifact-export skills fail.

    P1.1 unified design tokens: colors come from a ``DeckTheme``'s CSS
    custom properties (``--zl-*``) so the HTML report shares ONE token
    source with the PPTX/DOCX renderers — including the tenant's brand
    kit when one is configured.  ``theme=None`` falls back to the
    default zhanlu-blue tokens.
    """
    if theme is None:
        from app.services.artifacts.exporters._theme import load_theme
        theme = load_theme(None)
    css_vars = theme.as_css_block()
    rows = payload.chart.data if payload.chart else []
    if rows and payload.chart:
        headers = list(rows[0].keys())
        head = "".join(f"<th>{h}</th>" for h in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{str(r.get(h, ''))}</td>" for h in headers) + "</tr>"
            for r in rows[:200]
        )
        table_html = f"<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>{head}{body}</table>"
    else:
        table_html = "<p><em>No tabular data.</em></p>"

    kpi_html = "".join(
        f"<div class='kpi'><div class='label'>{k.label}</div><div class='value'>{k.value}</div>"
        + (f"<div class='caption'>{k.caption}</div>" if k.caption else "")
        + "</div>"
        for k in payload.kpis
    )
    insight_html = "<ul>" + "".join(f"<li>{i.text}</li>" for i in payload.insights) + "</ul>"

    sql_block = (
        f"<details><summary>SQL</summary><pre><code>{sql}</code></pre></details>"
        if sql
        else ""
    )

    return f"""<!doctype html>
<html><head><meta charset='utf-8'><title>{payload.title}</title>
<style>
  {css_vars}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          margin: 32px; color: var(--zl-text, #0F172A); max-width: 960px;
          background: var(--zl-bg, #ffffff); }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .source {{ color: var(--zl-muted, #64748B); font-size: 12px; margin-bottom: 24px; }}
  .summary {{ background: var(--zl-surface, #F1F5F9); padding: 12px 16px; border-radius: 8px; margin: 16px 0 24px; }}
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px; margin-bottom: 24px; }}
  .kpi {{ border: 1px solid var(--zl-border, #E2E8F0); border-radius: 8px; padding: 12px;
          background: var(--zl-kpi-bg, #F1F5F9); }}
  .kpi .label {{ font-size: 11px; color: var(--zl-muted, #64748B); text-transform: uppercase; letter-spacing: 0.5px; }}
  .kpi .value {{ font-size: 22px; font-weight: 600; color: var(--zl-text, #0F172A); margin-top: 2px; }}
  .kpi .caption {{ font-size: 11px; color: var(--zl-muted, #94A3B8); margin-top: 4px; }}
  table {{ width: 100%; font-size: 12px; margin-top: 8px; }}
  th {{ background: var(--zl-band-bg, #F8FAFC); text-align: left; }}
  td, th {{ padding: 4px 8px; }}
  details {{ margin-top: 16px; font-size: 12px; color: var(--zl-muted, #475569); }}
  pre {{ background: var(--zl-text, #0F172A); color: var(--zl-border, #E2E8F0); padding: 8px; border-radius: 4px; overflow-x: auto; }}
</style></head>
<body>
  <h1>{payload.title}</h1>
  <div class='source'>Source: {source or 'unknown'} · Generated {payload.generated_at}</div>
  <div class='summary'>{payload.summary}</div>
  <div class='kpis'>{kpi_html}</div>
  <h2>Insights</h2>
  {insight_html}
  <h2>Data</h2>
  {table_html}
  {sql_block}
  <hr style='margin-top:32px;border:none;border-top:1px solid #E2E8F0'>
  <footer style='font-size:11px;color:#94A3B8'>Zhanlu report · user_signal={payload.user_signal}</footer>
</body></html>"""


def finalize_into_artifact(
    db: Session,
    *,
    conversation_id: Optional[str],
    agent_name: Optional[str],
    user_message: str,
    source: Optional[str],
    sql: Optional[str],
    payload: ReportCardPayload,
    message_id: Optional[str] = None,
    org_id: Optional[str] = None,
    app_id: Optional[str] = None,
) -> Tuple[Optional[Artifact], dict[str, dict]]:
    """Persist an Artifact row (kind=html_report) with version + HTML blob.

    The HTML stored as an ``ArtifactBlob`` (blob_type="original") on a
    freshly created ``ArtifactVersion`` so the inline-preview API
    (``GET /artifacts/{id}/preview``) can serve it.  A
    ``MessageArtifact`` link is also created so the frontend can
    discover the artifact and render ``ArtifactPreviewCard.jsx``.

    Returns ``(artifact, file_exports)`` where:
    - ``artifact`` is the HTML artifact, or None if the DB write fails
    - ``file_exports`` is a dict keyed by format (e.g. ``"docx"``,
      ``"pptx"``) each containing ``{"artifact_id", "preview_url",
      "download_url", "job_id", "user_signal"}``.  Empty dict when no
      file-format intent was detected or the export failed.

    Multi-tenant (SaaS): ``org_id`` / ``app_id`` select the workspace
    brand kit that themes the HTML report and downstream file exports.
    Defaults preserve single-tenant behavior.

    FINALIZE failures must never take down the whole chat turn.
    """
    try:
        org = org_id or "default-org"
        app = app_id or "default-app"

        # P1.2/P1.1: resolve the workspace brand kit → DeckTheme so the
        # HTML report renders in the tenant's brand tokens.
        theme = None
        try:
            from app.services.artifacts.brand_kit import get_brand_kit
            from app.services.artifacts.exporters._theme import theme_from_brand_kit

            kit = get_brand_kit(db, org_id=org, app_id=app)
            theme = theme_from_brand_kit(kit) if kit else None
        except Exception as e:
            logger.debug("FINALIZE: brand kit resolution skipped: %s", e)

        html = _render_html_report(
            payload, user_message=user_message, source=source or "", sql=sql,
            theme=theme,
        )
        html_bytes = html.encode("utf-8")
        checksum = hashlib.sha256(html_bytes).hexdigest()

        # P0.2 outline gate: build the deterministic outline spec BEFORE
        # any heavyweight export rendering.  "auto" (default) records the
        # outline and continues; "block" parks the artifact in draft until
        # an approval flips outline.approved (UI hook); "off" skips.
        from app.services.artifacts.outline import build_outline, outline_gate_mode

        outline = None
        gate = outline_gate_mode()
        if gate != "off":
            try:
                outline = build_outline(payload)
            except Exception as e:
                logger.debug("FINALIZE: outline build skipped: %s", e)
        outline_blocked = bool(outline) and gate == "block"

        artifact = Artifact(
            id=str(uuid4()),
            conversation_id=conversation_id,
            created_by_agent_id=agent_name,
            artifact_type="html_report",
            title=payload.title,
            description=payload.summary,
            status="draft" if outline_blocked else "preview_ready",
            org_id=org,
            app_id=app,
            visibility="conversation_private",
            tags=["report", "synexia-fsm", payload.user_signal],
            metadata_json={
                "report_card_payload": payload.model_dump(),
                "source": source or "",
                "sql": sql,
                "user_message": user_message,
                "user_signal": payload.user_signal,
                "payload_formats": {},  # populated by task 6 (artifact-export-skills)
                **({"outline": outline} if outline else {}),
                **({"brand_kit_applied": theme.name} if theme else {}),
            },
            data_snapshot_ids=None,
        )
        db.add(artifact)
        db.flush()

        # Create version v1 and link it as the current version
        version = ArtifactVersion(
            id=str(uuid4()),
            artifact_id=artifact.id,
            version_number=1,
            status="preview_ready",
            built_at=datetime.now(timezone.utc),
        )
        db.add(version)

        # Store the HTML report as the original blob
        blob = ArtifactBlob(
            id=str(uuid4()),
            version_id=version.id,
            blob_type="original",
            file_name="report.html",
            mime_type="text/html",
            file_size=len(html_bytes),
            checksum=checksum,
            data=html_bytes,
        )
        db.add(blob)

        artifact.current_version_id = version.id
        db.flush()

        # Link the artifact to the chat message so the frontend can
        # discover it and render ArtifactPreviewCard.jsx
        if message_id and conversation_id:
            link = MessageArtifact(
                id=str(uuid4()),
                artifact_id=artifact.id,
                message_id=message_id,
                conversation_id=conversation_id,
                display_order=0,
            )
            db.add(link)
            db.flush()

        # Eager-render the user's likely-needed format so the first
        # download from the in-chat card is instant.  This is a
        # best-effort side-effect — if the renderer fails (e.g. the
        # optional library isn't installed) the in-chat card still
        # works and the user gets the HTML report on download.
        # Skipped while the outline gate is blocking (P0.2 "block" mode):
        # no heavyweight rendering until the outline is approved.
        if outline_blocked:
            logger.info(
                "FINALIZE: outline gate=block — eager render deferred for artifact %s",
                artifact.id,
            )
        else:
            eager_fmt: Optional[str] = None
            try:
                exporter = ExportService(db)
                eager_fmt = exporter.eager_render_default(
                    artifact,
                    user_message=user_message,
                    user_signal=payload.user_signal,
                    sql=sql,
                    source=source,
                    org_id=org,
                    app_id=app,
                )
                if eager_fmt:
                    logger.info(
                        "FINALIZE: eager-rendered %s for artifact %s",
                        eager_fmt, artifact.id,
                    )
            except Exception as e:
                logger.warning("FINALIZE: eager render failed (non-fatal): %s", e)

        # ----------------------------------------------------------------
        # File-format intent: if the user asked for a specific file
        # format (docx / pptx / xlsx / pdf / md), auto-trigger
        # ``run_sandbox_skill`` to produce the artifact in one turn.
        # ----------------------------------------------------------------
        file_exports: dict[str, dict] = {}
        try:
            from app.services.synexia.intent_router import (
                detect_file_intent,
                user_signal_for_format,
            )
            from app.services.tool_handlers.sandbox_tool import (
                run_sandbox_skill_sync,
            )

            requested_fmt = detect_file_intent(user_message)
            if requested_fmt:
                # ── Optimization: if eager_render_default already
                # cached a blob for the requested format on this
                # artifact, reuse it instead of spawning a sandbox.
                # The download endpoint GET /artifacts/{id}/download
                # ?format={fmt} will serve the cached blob.
                if requested_fmt == eager_fmt:
                    new_signal = user_signal_for_format(requested_fmt)
                    payload.user_signal = new_signal
                    artifact.metadata_json = dict(artifact.metadata_json or {})
                    artifact.metadata_json["payload_formats"] = dict(
                        artifact.metadata_json.get("payload_formats") or {}
                    )
                    export_entry = {
                        "artifact_id": artifact.id,
                        "preview_url": None,
                        "download_url": None,
                        "job_id": None,
                        "user_signal": new_signal,
                    }
                    artifact.metadata_json["payload_formats"][requested_fmt] = export_entry
                    file_exports[requested_fmt] = export_entry
                    artifact.tags = list(artifact.tags or []) + [
                        f"file_export:{requested_fmt}",
                    ]
                    db.add(artifact)
                    db.flush()
                    logger.info(
                        "FINALIZE: reusing cached %s blob for artifact %s (skip sandbox)",
                        requested_fmt, artifact.id,
                    )
                else:
                    chart_rows = (payload.chart.data if payload.chart else []) or []
                    if not chart_rows:
                        # Try to use the KPI table if no chart data is present
                        # (e.g. text-only reports).  We synthesize one row per
                        # KPI so the DOCX/PPTX/XLSX has something to display.
                        chart_rows = [
                            {"label": k.label, "value": k.value, **( {"caption": k.caption} if k.caption else {} )}
                            for k in (payload.kpis or [])
                        ]

                    instructions = (
                        f"Generate a {requested_fmt.upper()} report titled "
                        f"{payload.title!r} from the provided data. "
                        f"Source: {source or 'unknown'}. "
                        f"Summary: {payload.summary}"
                    )

                    sandbox_result = run_sandbox_skill_sync(
                        args={
                            "format": requested_fmt,
                            "data": chart_rows,
                            "title": payload.title,
                            "instructions": instructions,
                            # Pass through the KPI list and insights so the
                            # sandbox runner can render a styled KPI table
                            # and bullet list in the DOCX (Layer 3) and the
                            # sidecar rich-HTML preview (Layer 2).  We
                            # serialize pydantic objects to plain dicts
                            # because config.json is JSON-serialized for
                            # the Docker job.
                            "kpis": [
                                {
                                    "label": k.label,
                                    "value": k.value,
                                    **({"caption": k.caption} if k.caption else {}),
                                }
                                for k in (payload.kpis or [])
                            ],
                            # NOTE: serialize InsightSpec -> dict. Passing raw
                            # pydantic objects here raised "Object of type
                            # InsightSpec is not JSON serializable" inside
                            # run_sandbox_skill_sync (config.json), poisoned
                            # the SQLAlchemy session via a failed flush, and
                            # made the WHOLE artifact write roll back
                            # (2026-08-21, 82 occurrences in 24h of logs).
                            "insights": [
                                {"icon": i.icon, "text": i.text}
                                for i in (payload.insights or [])
                            ],
                            "source": source or "",
                            # C-Heavy skill-driven additions: the runner
                            # uses the original user request + the full
                            # ReportCardPayload to plan a custom document
                            # structure (rather than the previous fixed
                            # layout).  Falls back to ``instructions`` /
                            # ``kpis`` / ``insights`` for the deterministic
                            # generator when the LLM path is unavailable.
                            "user_message": user_message or instructions or "",
                            "synthesized_payload": (
                                payload.model_dump(mode="json")
                                if hasattr(payload, "model_dump")
                                else (
                                    payload.model_dump()
                                    if hasattr(payload, "model_dump")
                                    else (payload.dict() if hasattr(payload, "dict") else {})
                                )
                            ),
                        },
                        db=db,
                        user_id=agent_name,
                        context={
                            "conversation_id": conversation_id,
                            "agent_app_id": agent_name,
                        },
                    )

                    if sandbox_result.get("success"):
                        new_artifact_id = sandbox_result.get("artifact_id")
                        new_signal = user_signal_for_format(requested_fmt)
                        payload.user_signal = new_signal
                        artifact.metadata_json = dict(artifact.metadata_json or {})
                        artifact.metadata_json["payload_formats"] = dict(
                            artifact.metadata_json.get("payload_formats") or {}
                        )
                        export_entry = {
                            "artifact_id": new_artifact_id,
                            "preview_url": sandbox_result.get("preview_url"),
                            "download_url": sandbox_result.get("download_url"),
                            "job_id": sandbox_result.get("job_id"),
                            "user_signal": new_signal,
                        }
                        artifact.metadata_json["payload_formats"][requested_fmt] = export_entry
                        # Build the returned file_exports dict so callers
                        # (agents.py) can propagate it to the frontend
                        # without an extra DB read.
                        file_exports[requested_fmt] = export_entry
                        # Mirror the new artifact_id on the artifact itself
                        # so chat-loop code that returns ``artifact_id``
                        # picks it up without an extra read.
                        artifact.tags = list(artifact.tags or []) + [
                            f"file_export:{requested_fmt}",
                        ]
                        db.add(artifact)
                        db.flush()
                        logger.info(
                            "FINALIZE: file-format intent=%s → sandbox artifact %s",
                            requested_fmt, new_artifact_id,
                        )
                    else:
                        logger.warning(
                            "FINALIZE: run_sandbox_skill returned error for %s: %s",
                            requested_fmt, sandbox_result.get("error"),
                        )
        except Exception as e:
            # Auto-export is best-effort.  The in-chat ReportCard still
            # works; the user can fall back to the export menu on the
            # card itself.
            logger.warning(
                "FINALIZE: file-format auto-export failed (non-fatal): %s", e,
            )

        logger.info(
            "FINALIZE: created Artifact %s (kind=html_report, title=%r, version=%d, blob=%d bytes)",
            artifact.id, payload.title, version.version_number, len(html_bytes),
        )
        return artifact, file_exports
    except Exception as e:
        logger.warning("FINALIZE: artifact write failed (non-fatal): %s", e)
        # Roll back the pending add so it doesn't poison the next call.
        try:
            db.rollback()
        except Exception:
            pass
        return None, {}


def fsm_finalize_into_artifact(
    db: Session,
    *,
    conversation_id: Optional[str],
    agent_name: Optional[str],
    user_message: str,
    observations: list[DBObservationRecord],
    task_spec: Optional[dict] = None,
    message_id: Optional[str] = None,
) -> Optional[tuple[Optional[Artifact], dict[str, dict], Optional[ReportCardPayload]]]:
    """FINALIZE helper for the FSM path — builds artifacts from observations.

    This sibling of ``finalize_into_artifact()`` consumes FSM observation
    records instead of a pre-built ``ReportCardPayload``.  It:

    1. Finds the ``synthesize`` observation (if any) and extracts the
       report payload (title, summary, KPIs, chart, insights).
    2. Builds a ``ReportCardPayload`` from that data.
    3. Delegates to ``finalize_into_artifact()`` for the actual Artifact
       persistence + file-format auto-export.

    Returns ``(artifact, file_exports, report_card_payload)``, or ``None``
    if no synthesis data was found.
    """
    from datetime import datetime

    task_spec = task_spec or {}

    # ── 1. Find the synthesize observation ────────────────────────────
    synth_obs = None
    sandbox_obs = None
    for obs in observations:
        if obs.observation_type == "synthesize" and obs.success:
            synth_obs = obs
        if obs.observation_type == "sandbox" and obs.success:
            sandbox_obs = obs

    if not synth_obs:
        logger.debug("fsm_finalize_into_artifact: no synthesize observation found — skipping")
        return None

    synth_data = {}
    if synth_obs.result_data and isinstance(synth_obs.result_data, dict):
        synth_data = synth_obs.result_data.get("synth_data") or {}

    # ── 2. Build ReportCardPayload from synthesis ─────────────────────
    title = (
        synth_data.get("title")
        or task_spec.get("entities", {}).get("report_title")
        or (sandbox_obs.result_data.get("format", "").upper() + " Report" if sandbox_obs and sandbox_obs.result_data else "Report")
    )
    summary = synth_data.get("summary", "")
    instructions = synth_data.get("instructions", "")

    # KPIs
    kpis = []
    raw_kpis = synth_data.get("kpis") or []
    for k in raw_kpis:
        if isinstance(k, dict):
            kpis.append(KPISpec(
                label=str(k.get("label", "")),
                value=str(k.get("value", "")),
                caption=str(k.get("caption", "")) if k.get("caption") else None,
            ))

    # Chart
    from app.services.synexia.contracts import ChartSpec
    chart = None
    raw_chart = synth_data.get("chart")
    if raw_chart and isinstance(raw_chart, dict):
        try:
            chart = ChartSpec(
                type=raw_chart.get("type", "bar"),
                title=raw_chart.get("title", title),
                x_key=raw_chart.get("x_key", ""),
                y_keys=raw_chart.get("y_keys", []),
                data=raw_chart.get("data", []),
                unit=raw_chart.get("unit", ""),
            )
        except Exception as e:
            logger.debug("fsm_finalize: chart parse skipped: %s", e)

    # Insights
    insights = []
    raw_insights = synth_data.get("insights") or []
    for ins in raw_insights:
        if isinstance(ins, dict):
            insights.append(InsightSpec(
                icon=str(ins.get("icon", "lightbulb")),
                text=str(ins.get("text", "")),
            ))

    user_signal = task_spec.get("user_signal", "default")

    payload = ReportCardPayload(
        title=title,
        source=task_spec.get("entities", {}).get("source", ""),
        generated_at=datetime.now(timezone.utc).isoformat() + "Z",
        summary=summary,
        kpis=kpis,
        chart=chart,
        insights=insights or [InsightSpec(icon="info", text="Report generated from FSM pipeline.")],
        user_signal=user_signal,
    )

    # ── 3. Push through finalize_into_artifact ────────────────────────
    artifact, file_exports = finalize_into_artifact(
        db,
        conversation_id=conversation_id,
        agent_name=agent_name,
        user_message=user_message,
        source=None,
        sql=None,
        payload=payload,
        message_id=message_id,
    )

    return artifact, file_exports, payload


def build_tool_call_payload(
    finalize_result: FinalizeResult,
    *,
    artifact_id: Optional[str],
    file_export_artifact_id: Optional[str] = None,
    file_export_format: Optional[str] = None,
) -> dict[str, Any]:
    """Build the JSON payload that gets attached to the assistant tool_call.

    The frontend's MessageBubble inspects the tool_call for either:
    - `report_card_payload` (preferred, new): renders ReportCard.jsx
    - `rows` (fallback, old): renders DataTableCard.jsx
    - `artifact_id`: also fetches ArtifactPreviewCard.jsx

    If the user asked for a specific file format (docx / pptx / xlsx /
    pdf / md) and the Synexia FSM auto-triggered ``run_sandbox_skill``,
    ``file_export_artifact_id`` is the new artifact's id.  The
    frontend uses this to mount ``ArtifactPreviewCard`` in the same
    message bubble as the ``ReportCard`` so the user gets the
    downloadable file without a follow-up click.
    """
    payload: dict[str, Any] = {
        "type": "report_card",
        "task_kind": finalize_result.task_kind,
        "user_signal": finalize_result.user_signal,
        "report_card_payload": (
            finalize_result.report_card_payload.model_dump()
            if finalize_result.report_card_payload is not None
            else None
        ),
    }
    if artifact_id:
        payload["artifact_id"] = artifact_id
    if file_export_artifact_id:
        payload["file_export_artifact_id"] = file_export_artifact_id
        payload["file_export_format"] = file_export_format
        # Mirror the file-export artifact id under the regular
        # ``artifact_id`` key as well so existing frontend paths
        # (``MessageBubble`` checks for ``artifact_id`` to mount
        # ``ArtifactPreviewCard``) just work without a code change.
        payload["artifact_id"] = file_export_artifact_id
    return payload
