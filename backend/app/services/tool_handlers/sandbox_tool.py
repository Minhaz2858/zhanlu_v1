"""run_sandbox_skill — generate file artifacts from data via Docker sandbox.

This tool bridges the chat agent to the existing sandbox execution pipeline.
When an agent has real data rows (from ask_data_agent) and needs to produce
a downloadable file (xlsx, pptx, html dashboard, pdf), it calls this tool.

The handler:
1. Creates an Artifact + ArtifactVersion (draft → building)
2. Builds an input package (data snapshots + instructions + format)
3. Writes the sandbox_runner.py script into the input package
4. Calls SandboxService.create_job() which enqueues to Redis
5. Polls job status every 1s until completed/failed/timeout (max 120s)
6. Returns artifact_id + preview/download URLs to the LLM

Architecture invariants respected:
- SBX-1: Only sandbox-worker creates containers. This handler only creates job records.
- SBX-4: Sandbox receives data as JSON snapshots, never DB credentials.
- SBX-11: Outputs stored as PostgreSQL-backed artifact versions.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

# Path to the generic sandbox runner script — gets mounted into the container
_RUNNER_SCRIPT_PATH = Path(__file__).parent.parent / "sandbox" / "sandbox_runner.py"

# Image mapping by output format
_IMAGE_BY_FORMAT = {
    "xlsx": "zhanlu-sandbox-python:latest",
    "pptx": "zhanlu-sandbox-python:latest",
    "html": "zhanlu-sandbox-python:latest",
    "pdf":  "zhanlu-sandbox-python:latest",
    "docx": "zhanlu-sandbox-python:latest",
    "md":   "zhanlu-sandbox-python:latest",
}

# Artifact type mapping
_ARTIFACT_TYPE_BY_FORMAT = {
    "xlsx": "xlsx",
    "pptx": "pptx",
    "html": "html",
    "pdf":  "pdf",
    "docx": "docx",
    "md":   "md",
}

# MIME type mapping
_MIME_BY_FORMAT = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "html": "text/html",
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md":   "text/markdown",
}

# File extension mapping
_EXT_BY_FORMAT = {
    "xlsx": "xlsx",
    "pptx": "pptx",
    "html": "html",
    "pdf":  "pdf",
    "docx": "docx",
    "md":   "md",
}

# Max poll time for the tool handler (seconds)
_MAX_POLL_SECONDS = 120
_POLL_INTERVAL_SECONDS = 1.0


def _enrich_args_from_sibling_html_report(
    *,
    args: dict,
    db: Session,
    conversation_id: str,
    title: str,
) -> dict:
    """Fill in Claude-style content fields on ``args`` from a sibling
    ``html_report`` artifact in the same conversation.

    When the agent calls ``finalize_into_artifact`` first (which
    produces a rich HTML report card) and then ``run_sandbox_skill``
    to convert that report into a downloadable file, the rich
    ``ReportCardPayload`` (summary / methodology / kpis / insights /
    recommendations / sql / next_step) is stored on the
    ``html_report`` artifact's ``source_json.rcp`` field.  This
    helper picks the most recent title-matching ``html_report`` in
    the same conversation and copies any blank field on ``args``
    from it so the docx/pptx/xlsx renders the same rich content as
    the HTML report the user just saw.
    """
    try:
        from app.models.artifact import Artifact, ArtifactVersion
        from sqlalchemy import desc

        # The "rich" sibling could be one of:
        #   - artifact_type='html_report'  — produced by
        #     ``finalize_into_artifact`` and carries the full rcp in
        #     ``artifact.metadata_json["report_card_payload"]``.
        #   - artifact_type='html'         — the sidecar produced by
        #     the sandbox's ``generate_html`` for a previous
        #     ``run_sandbox_skill(format='html', ...)`` call.  This
        #     sidecar is also rich (KPI cards, charts, etc.) but the
        #     rcp lives in ``version.source_json.rcp``.
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
            return args

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

        def _extract_rcp(art: Artifact) -> dict | None:
            """Pull the rcp out of an artifact, returning a non-empty
            dict or None.  An empty rcp (everything blank) is treated
            as missing so we don't accidentally pick a sparse sidecar
            that just happens to match the title."""
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
            # Treat an effectively-empty rcp (no summary, no kpis, no
            # insights, no key_findings, no recommendations) as missing.
            if not any(rcp.get(k) for k in (
                "summary", "methodology", "kpis", "insights",
                "key_findings", "recommendations", "sections",
                "next_step", "sql",
            )):
                return None
            return rcp

        # Prefer a title-matched sibling that has a real rcp.
        sibling = next(
            (s for s in siblings if _matches(_norm(s.title)) and _extract_rcp(s)),
            None,
        )
        # Fall back to the most-recent sibling with a real rcp.
        if sibling is None:
            sibling = next((s for s in siblings if _extract_rcp(s)), None)
        if sibling is None:
            return args

        rcp = _extract_rcp(sibling)
        if not isinstance(rcp, dict) or not rcp:
            return args

        # Only fill fields the agent left blank so we never clobber a
        # more-specific instruction.
        filled = []
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
            if not args.get(k) and rcp.get(k):
                args[k] = rcp[k]
                filled.append(k)
        if filled:
            logger.info(
                "Enriched sandbox args from sibling %s (title=%r, type=%s): filled %s",
                sibling.id, sibling.title, sibling.artifact_type, filled,
            )
        return args
    except Exception as enrich_err:
        # Enrichment is best-effort; never fail the file generation
        # just because we couldn't find a sibling.
        logger.warning(
            "Could not enrich sandbox args from sibling html_report: %s",
            enrich_err,
        )
        return args


def run_sandbox_skill_sync(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Synchronous public entry point for the sandbox skill.

    Wraps the existing async ``_run_sandbox_skill`` so synchronous
    callers (Synexia FSM ``finalize_into_artifact``, FastAPI routes
    that aren't ``async``, etc.) can trigger the same pipeline
    without bridging an event loop.

    Behavior is identical to the tool path: validates input, creates
    Artifact + Version, enqueues the sandbox job to Redis, polls
    until completion, and returns the artifact info.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside an async context — schedule and wait via
            # ``asyncio.run_coroutine_threadsafe`` semantics.  In
            # practice the FSM finalize path runs from a sync
            # FastAPI route, so this branch is rarely taken; we
            # fall through to the ``run_until_complete`` path below
            # for that case.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _run_sandbox_skill(args, db, user_id, context),
                )
                return future.result(timeout=_MAX_POLL_SECONDS + 30)
        return loop.run_until_complete(
            _run_sandbox_skill(args, db, user_id, context)
        )
    except RuntimeError:
        # No event loop in the current thread — run one ourselves.
        return asyncio.run(_run_sandbox_skill(args, db, user_id, context))


async def _run_sandbox_skill(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Generate a file artifact from data via the Docker sandbox.

    Flow:
        1. Validate inputs
        2. Create Artifact + ArtifactVersion
        3. Build input package (data snapshots + runner script + instructions)
        4. Create SandboxJob (enqueued to Redis)
        5. Poll job status until complete
        6. Return artifact info to LLM
    """
    fmt = (args.get("format") or "").lower().strip()
    # "dashboard" is a user-facing alias: it renders as an interactive
    # HTML artifact (KPI cards + chart + table), so map it to ``html``.
    if fmt == "dashboard":
        fmt = "html"
    if fmt not in _IMAGE_BY_FORMAT:
        return {
            "success": False,
            "error": f"Invalid format '{fmt}'. Must be one of: {list(_IMAGE_BY_FORMAT.keys())}",
        }

    data = args.get("data")
    if not data or not isinstance(data, list):
        return {"success": False, "error": "data must be a non-empty array of objects"}

    title = (args.get("title") or "Generated Report").strip()
    instructions = (args.get("instructions") or "").strip()
    conversation_id = (context or {}).get("conversation_id")
    agent_app_id = (context or {}).get("agent_app_id")

    # --- 0. Enrich sparse args from a sibling rich HTML report ---
    # The agent often calls run_sandbox_skill right after
    # ``finalize_into_artifact`` to convert the rich HTML report
    # into a downloadable file.  In that case the rich HTML report
    # already carries the Executive Summary / Methodology / KPIs /
    # Insights / Recommendations / SQL / Next Step fields, but the
    # sandbox call's args are usually just ``{data, title,
    # instructions}``.  Without this enrichment the resulting
    # docx/pptx/xlsx is just a title + bare data table, which is
    # exactly the "docx is empty" complaint the user reported.
    # We look up a recent ``html_report`` artifact in the same
    # conversation whose title matches, and merge its
    # ``source_json.rcp`` into ``args`` for any field the agent
    # left blank.
    if conversation_id and fmt in ("docx", "pptx", "xlsx"):
        args = _enrich_args_from_sibling_html_report(
            args=args,
            db=db,
            conversation_id=conversation_id,
            title=title,
        )

    # --- 1. Create Artifact + Version ---
    from app.services.artifacts.artifact_service import ArtifactService
    artifact_service = ArtifactService(db)

    artifact_type = _ARTIFACT_TYPE_BY_FORMAT[fmt]
    artifact = artifact_service.create_artifact(
        artifact_type=artifact_type,
        title=title,
        conversation_id=conversation_id,
        created_by_agent_id=agent_app_id,
        description=f"Generated from {len(data)} data rows",
    )

    version = artifact_service.create_version(
        artifact_id=artifact.id,
        changelog=f"Initial generation ({fmt.upper()})",
        produced_by_skill="sandbox_runner",
    )

    # --- 2. Read the runner script ---
    try:
        runner_script = _RUNNER_SCRIPT_PATH.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed to read sandbox_runner.py: %s", e)
        return {"success": False, "error": f"Runner script not found: {e}"}

    # --- 3. Build input package ---
    # Claude-style content fields are forwarded to the sandbox
    # generators (generate_docx / generate_pptx) so they can render
    # the full Executive Summary / Methodology / Key Findings /
    # Recommendations / SQL layout instead of a bare title + data
    # table.  Falls back to legacy kpis/insights when not provided.
    kpis = args.get("kpis") or []
    insights = args.get("insights") or []
    source = args.get("source") or ""
    summary = args.get("summary") or ""
    methodology = args.get("methodology") or ""
    key_findings = args.get("key_findings") or []
    recommendations = args.get("recommendations") or []
    sections = args.get("sections") or []
    sql_text = args.get("sql") or ""
    next_step = args.get("next_step") or ""
    # chart + warnings are forwarded so generate_pptx / generate_docx can
    # render a native chart slide and an amber warnings box respectively.
    chart = args.get("chart")
    warnings = args.get("warnings") or []
    input_package = {
        "skill_config": {
            "format": fmt,
            "title": title,
            "instructions": instructions,
            "row_count": len(data),
            "kpis": kpis,
            "insights": insights,
            "source": source,
            # v2 Claude-style fields
            "summary": summary,
            "methodology": methodology,
            "key_findings": key_findings,
            "recommendations": recommendations,
            "sections": sections,
            "sql": sql_text,
            "next_step": next_step,
            "chart": chart,
            "warnings": warnings,
        },
        "data_snapshots": [
            {
                "name": "query_results",
                "data": data,
                "format": "json",
            }
        ],
        "instructions": instructions or f"Generate a {fmt.upper()} report titled '{title}' from the provided data.",
        "runner_script": base64.b64encode(runner_script.encode("utf-8")).decode("ascii"),
        "runner_script_name": "sandbox_runner.py",
    }

    output_spec = {
        "format": fmt,
        "expected_files": [f"report.{_EXT_BY_FORMAT[fmt]}"],
    }

    # --- 4. Create sandbox job ---
    from app.services.sandbox.sandbox_service import SandboxService
    sandbox_service = SandboxService(db)

    job = sandbox_service.create_job(
        skill_name="sandbox_runner",
        artifact_id=artifact.id,
        artifact_version_id=version.id,
        conversation_id=conversation_id,
        input_package=input_package,
        output_spec=output_spec,
        timeout_seconds=120,
        image_name=_IMAGE_BY_FORMAT[fmt],
    )

    logger.info(
        "Created sandbox job %s for artifact %s (format=%s, rows=%d)",
        job.id, artifact.id, fmt, len(data),
    )

    # --- 5. Poll for completion ---
    elapsed = 0.0
    while elapsed < _MAX_POLL_SECONDS:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS

        # Refresh job status from DB
        db.expire_all()
        current_job = sandbox_service.get_job(job.id)
        if not current_job:
            break

        status = current_job.status
        if status in ("completed", "failed", "timeout", "cancelled"):
            break

    # --- 6. Build result ---
    final_job = sandbox_service.get_job(job.id)
    if not final_job:
        return {"success": False, "error": "Sandbox job disappeared during polling"}

    if final_job.status != "completed":
        error_msg = final_job.error_message or f"Sandbox job ended with status '{final_job.status}'"
        # Mark artifact version as failed
        artifact_service.mark_version_failed(version.id, error_msg)
        return {
            "success": False,
            "error": error_msg,
            "job_id": job.id,
            "job_status": final_job.status,
            "artifact_id": artifact.id,
        }

    # Job completed — check for stored blobs
    blobs = artifact_service.get_version_blobs(version.id, blob_type="original")

    files_info = []
    for blob in blobs:
        files_info.append({
            "file_name": blob.file_name,
            "file_size": blob.file_size,
            "mime_type": blob.mime_type,
        })

    # Canonical create_artifact-compatible keys so the turn pipeline's
    # artifact collector surfaces sandbox output identically to the
    # direct/exporter path (one render path, swappable engines).
    primary_blob = blobs[0] if blobs else None
    canonical_file_name = (
        primary_blob.file_name if primary_blob and primary_blob.file_name
        else f"report.{_EXT_BY_FORMAT[fmt]}"
    )
    canonical_mime = (
        primary_blob.mime_type if primary_blob and primary_blob.mime_type
        else _MIME_BY_FORMAT[fmt]
    )
    canonical_size = primary_blob.file_size if primary_blob else None

    # ------------------------------------------------------------------
    # Sidecar rich HTML preview (Layer 2 of the "one card per file
    # format" plan).  For docx/pptx/xlsx we generate an interactive
    # Plotly-dashboard HTML artifact in-process and link it to the
    # file-format artifact via ``metadata_json.preview_artifact_id``.
    # The frontend ``ArtifactPreviewPane`` will then iframe the
    # sidecar's preview URL instead of falling back to the plain
    # mammoth rendering.
    # ------------------------------------------------------------------
    preview_artifact_id: Optional[str] = None
    if fmt in ("docx", "pptx", "xlsx"):
        try:
            # Build the same data/config dicts the sandbox runner uses
            # so the sidecar matches the file content 1:1.
            sidecar_config = {
                "format": fmt,
                "title": title,
                "instructions": instructions,
                "kpis": kpis,
                "insights": insights,
                "source": source,
                # v2 Claude-style fields (so the sidecar dashboard
                # surfaces the same rich sections as the docx).
                "summary": summary,
                "methodology": methodology,
                "key_findings": key_findings,
                "recommendations": recommendations,
                "sections": sections,
                "sql": sql_text,
                "next_step": next_step,
            }
            # ``generate_html`` is the same function the sandbox
            # Docker job calls; it writes to ``OUTPUT_DIR`` (=/output
            # inside the container) and returns the filename.  We
            # need the actual bytes for the sidecar blob, so we run
            # it under a temporary OUTPUT_DIR and read the file back.
            import tempfile
            from pathlib import Path
            from app.services.sandbox import sandbox_runner as _sr
            with tempfile.TemporaryDirectory(prefix="zhanlu-sidecar-") as tmp:
                original_output_dir = _sr.OUTPUT_DIR
                _sr.OUTPUT_DIR = Path(tmp)
                try:
                    _sr.generate_html(data, sidecar_config, instructions)
                    sidecar_path = Path(tmp) / "report.html"
                    sidecar_html = sidecar_path.read_text(encoding="utf-8")
                finally:
                    _sr.OUTPUT_DIR = original_output_dir

            # Fresh sidecar artifact + version + blob.
            sidecar = artifact_service.create_artifact(
                artifact_type="html",
                title=f"{title} (preview)",
                conversation_id=conversation_id,
                created_by_agent_id=agent_app_id,
                description="Rich interactive preview generated alongside the file-format artifact.",
            )
            sidecar_version = artifact_service.create_version(
                artifact_id=sidecar.id,
                changelog="Rich HTML sidecar preview",
                produced_by_skill="sandbox_runner_sidecar",
            )
            sidecar_file_name = f"{title}-preview.html"
            artifact_service.store_blob(
                sidecar_version.id,
                blob_type="original",
                file_name=sidecar_file_name,
                mime_type="text/html",
                data=sidecar_html.encode("utf-8"),
            )
            # Mark the sidecar as preview-ready immediately — the bytes
            # were generated in-process and don't need a Docker job.
            artifact_service.mark_version_built(sidecar_version.id)

            # Link the file-format artifact → sidecar via metadata.
            # Note: ``mark_version_built`` already issued an explicit
            # commit on the sidecar's version, so the parent artifact
            # row's update is still in this session's transaction.  We
            # flush and commit here to make the linkage visible to the
            # chat response collector (``_collect_artifact_results``)
            # and the frontend, even when the caller's outer
            # transaction is later rolled back.
            artifact = artifact_service.get_artifact(artifact.id)
            if artifact is not None:
                md = dict(artifact.metadata_json or {})
                md["preview_artifact_id"] = sidecar.id
                artifact.metadata_json = md
                db.add(artifact)
                db.flush()
                db.commit()

            preview_artifact_id = sidecar.id
            logger.info(
                "Sidecar HTML preview %s created for %s artifact %s",
                sidecar.id, fmt, artifact.id,
            )
        except Exception as sidecar_err:
            # Non-fatal: the file format artifact still works without
            # the rich preview.  Frontend will fall back to mammoth
            # for docx or no-preview for pptx/xlsx.
            logger.warning(
                "Failed to generate sidecar HTML preview for %s artifact %s: %s",
                fmt, artifact.id, sidecar_err,
            )

    return {
        "success": True,
        "artifact_id": artifact.id,
        "artifact_version_id": version.id,
        # canonical keys (same names as create_artifact results)
        "version_id": version.id,
        "version_number": version.version_number,
        "file_url": f"/api/artifacts/{artifact.id}/download",
        "type": fmt,
        "file_name": canonical_file_name,
        "mime_type": canonical_mime,
        "file_size": canonical_size,
        "has_preview": bool(blobs),
        "title": title,
        "format": fmt,
        "files": files_info,
        "preview_url": f"/api/artifacts/{artifact.id}/preview",
        "download_url": f"/api/artifacts/{artifact.id}/download",
        # Sidecar rich-preview linkage (Layer 2).  Frontend
        # ArtifactPreviewPane prefers this over the file-format
        # artifact's own preview endpoint.
        "preview_artifact_id": preview_artifact_id,
        "job_id": job.id,
        "message": (
            f"Artifact generated successfully: {title} ({fmt.upper()}). "
            f"The file is stored as a versioned artifact. "
            f"Tell the user they can preview or download it using the artifact card. "
            f"Preview URL: /api/artifacts/{artifact.id}/preview, "
            f"Download URL: /api/artifacts/{artifact.id}/download"
        ),
    }


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

RUN_SANDBOX_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_sandbox_skill",
        "description": (
            "Generate a file artifact (Excel, PPTX, HTML dashboard, or PDF) from data. "
            "Use this AFTER calling ask_data_agent to get real data rows. "
            "Pass the rows as the 'data' parameter. The file is generated in an "
            "isolated Docker sandbox and stored as a versioned artifact with "
            "preview and download URLs. "
            "Supported formats: xlsx (spreadsheet), pptx (presentation), "
            "html (interactive dashboard with charts), pdf (document), docx (Word), md (markdown)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["xlsx", "pptx", "html", "pdf", "docx", "md"],
                    "description": "Output file format.",
                },
                "data": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Data rows to include in the report. "
                        "Pass the 'rows' array from a prior ask_data_agent call."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Title for the generated file/report.",
                },
                "instructions": {
                    "type": "string",
                    "description": (
                        "Natural-language instructions for what to generate. "
                        "E.g. 'Create a weekly sales summary with totals and a chart' "
                        "or 'Make a dashboard with bar charts for revenue by month'."
                    ),
                },
            },
            "required": ["format", "data", "title"],
        },
    },
}

registry.register(
    name="run_sandbox_skill",
    schema=RUN_SANDBOX_SKILL_SCHEMA,
    handler=_run_sandbox_skill,
    category="sandbox",
    enabled_by_default=False,
    description="Generate a file artifact (xlsx/pptx/html/pdf) from data via Docker sandbox.",
)
