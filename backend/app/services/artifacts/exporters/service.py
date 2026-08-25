"""ExportService — orchestrates the artifact exporters against the DB.

Responsibilities:

1. Look up the cached format blob (if any) for an artifact.
2. If absent, run the renderer + persist the bytes as a new ``ArtifactBlob``
   with ``blob_type="format_export"``, then update the artifact's
   ``metadata_json["payload_formats"][format]`` to point at the blob.
3. Eager-render an artifact for the user's "preferred" format at
   finalize-time so the first download is instant.
4. Provide a single, well-typed entry point for the artifact router to
   call when ``?format=...`` is requested.

Why a service layer separate from the routers?  The routers
(`/api/artifacts/{id}/download?format=…`) are intentionally thin —
they parse params, call into this service, and serialize the
response.  All the "do we have a cached copy / do we need to render
/ how do we attach it to the right version" logic lives here, so
it's easy to unit-test and easy to call from other places (e.g.
the FINALIZE step eager-rendering a default format, or a background
job warming up all four formats for an artifact).

Public surface:

* ``ExportService.get_or_render(artifact, format) -> (bytes, mime, file_name)``
* ``ExportService.eager_render_default(artifact, *, preferred_format) -> Optional[str]``
* ``ExportService.list_available_formats(artifact) -> dict[format, blob_info]``

The service is *not* a singleton — instantiate per request as
``ExportService(db)``.  The DB session is the only state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.artifact import Artifact, ArtifactBlob, ArtifactVersion
from app.services.synexia.contracts import ReportCardPayload
from app.services.artifacts.exporters import (
    ExportContext,
    SUPPORTED_FORMATS,
    render,
    safe_file_extension,
    safe_mime_type,
)
from app.services.artifacts.exporters._common import chart_rows, stamp_filename
from app.services.artifacts.deck_data import build_deck_user_context, collect_grounded_rows

logger = logging.getLogger(__name__)


# `format_export` is a new value of ArtifactBlob.blob_type.  The model
# doesn't enforce an enum, but using a literal here makes the code
# self-documenting and gives us a single grep target.
BLOB_TYPE_FORMAT_EXPORT = "format_export"


# P0 enforcement loop toggle. By default the semantic audit is recorded on
# the artifact (metadata_json["audit_reports"][format]) and logged, but a
# FAIL does NOT block the download — strict rules would break too many real
# exports until the renderers are tuned. Set ZHANLU_AUDIT_BLOCK_ON_FAIL=1
# to raise ExportError on a FAIL status and stop the broken file shipping.
AUDIT_BLOCK_ON_FAIL = os.environ.get("ZHANLU_AUDIT_BLOCK_ON_FAIL", "0") == "1"


def _repair_cycles() -> int:
    """Max deterministic repair cycles after a FAIL audit (P0.1 self-healing).

    The loop is: render → audit → (FAIL → repair → re-audit) × N.  Set
    ZHANLU_AUDIT_REPAIR_CYCLES=0 to disable self-healing entirely.
    """
    try:
        return max(0, int(os.environ.get("ZHANLU_AUDIT_REPAIR_CYCLES", "2")))
    except ValueError:
        return 2


# What to render at finalize time, by user_signal.  If the user_signal
# isn't export-flavored, we don't pre-render (the in-chat card is the
# primary surface and downloads are best-effort).
DEFAULT_FORMAT_BY_SIGNAL: dict[str, str] = {
    "export": "pdf",
    "download": "pdf",
    "save": "xlsx",
}

# Format hint substrings we look for in the user message to upgrade
# the default format at finalize time.  Order matters — first match wins.
_FORMAT_HINT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("docx", ("docx", "doc", "word", "document", "公文", "报告文档")),
    ("pptx", ("pptx", "ppt", "presentation", "deck", "slides", "powerpoint")),
    ("xlsx", ("xlsx", "excel", "spreadsheet")),
    ("pdf", ("pdf",)),
    ("csv", ("csv",)),
]


class ExportError(Exception):
    """Raised when an export operation fails in a way the caller should surface."""


class ExportService:
    """Orchestrates the export renderers + artifact blob storage."""

    def __init__(self, db: Session):
        self.db = db

    # --- Public API --------------------------------------------------------

    def get_or_render(
        self,
        artifact: Artifact,
        format: str,
        *,
        user_message: str = "",
        sql: Optional[str] = None,
        source: Optional[str] = None,
        theme: Optional[str] = None,
        mode: Optional[str] = None,
        style_recipe: Optional[str] = None,
        doc_type: Optional[str] = None,
        org_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> tuple[bytes, str, str]:
        """Return the rendered file as ``(bytes, mime_type, file_name)``.

        If a cached format blob exists on the artifact's current
        version, return it.  Otherwise render, persist, cache, and
        return.

        When a non-default ``theme`` / ``mode`` / ``style_recipe`` / ``doc_type``
        is requested the cache is bypassed (the per-format cache key does
        not yet encode those knobs), so a non-default render is always
        fresh and is not persisted over the default-theme blob.

        Multi-tenant brand kits (SaaS): when no explicit theme knobs are
        given, the workspace's brand kit (``org_id`` / ``app_id`` — falling
        back to the artifact's own tenant columns) is resolved to flat
        theme tokens and takes precedence over the default ``zhanlu-blue``
        palette.  Brand renders ARE cached, keyed by a token fingerprint,
        so a kit edit automatically invalidates stale renders.
        """
        fmt = (format or "").lower().strip()
        if fmt not in SUPPORTED_FORMATS:
            raise ExportError(f"Unsupported export format: {format!r}")

        dt_norm = (doc_type or "report").strip().lower()
        if dt_norm not in ("report", "brief", "memo"):
            dt_norm = "report"
        themed = bool(theme or mode or style_recipe or dt_norm != "report")

        # Resolve the tenant brand kit (only when no explicit theme knobs).
        theme_tokens: Optional[dict] = None
        brand_fp: Optional[str] = None
        if not themed:
            theme_tokens, brand_fp = self._resolve_brand_tokens(
                artifact, org_id=org_id, app_id=app_id
            )

        # Try cache first (default theme + report doc_type only — other
        # variants are fresh).  Brand-kit renders consult the cache too,
        # but only accept the entry when its fingerprint matches the
        # current kit (a kit edit invalidates the old render).
        if not themed:
            cached = self._find_cached_format_blob(artifact, fmt)
            if cached is not None:
                if brand_fp is None:
                    data = self._blob_data(cached)
                    if data is not None:
                        return data, cached.mime_type, cached.file_name
                elif self._cached_fingerprint(artifact, fmt) == brand_fp:
                    data = self._blob_data(cached)
                    if data is not None:
                        return data, cached.mime_type, cached.file_name

        # Render (+ persist for the default / brand-kit theme).
        return self._render_and_store(
            artifact,
            fmt,
            user_message=user_message,
            sql=sql,
            source=source,
            theme=theme,
            mode=mode,
            style_recipe=style_recipe,
            doc_type=dt_norm,
            theme_tokens=theme_tokens,
            theme_fingerprint=brand_fp,
            persist=not themed,
        )

    def eager_render_default(
        self,
        artifact: Artifact,
        *,
        user_message: str = "",
        user_signal: str = "default",
        sql: Optional[str] = None,
        source: Optional[str] = None,
        theme: Optional[str] = None,
        mode: Optional[str] = None,
        style_recipe: Optional[str] = None,
        org_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> Optional[str]:
        """Pre-render the artifact's most-likely-needed format.

        Returns the format that was rendered (or None if no render was
        needed — e.g. the user_signal didn't imply a download).  Safe
        to call multiple times: if the format is already cached, this
        is a no-op.
        """
        fmt = self._pick_default_format(user_signal=user_signal, user_message=user_message)
        if fmt is None:
            return None

        # If the format is already cached, skip.
        if self._find_cached_format_blob(artifact, fmt) is not None:
            return fmt

        themed = bool(theme or mode or style_recipe)
        theme_tokens: Optional[dict] = None
        brand_fp: Optional[str] = None
        if not themed:
            theme_tokens, brand_fp = self._resolve_brand_tokens(
                artifact, org_id=org_id, app_id=app_id
            )

        try:
            self._render_and_store(
                artifact,
                fmt,
                user_message=user_message,
                sql=sql,
                source=source,
                theme=theme,
                mode=mode,
                style_recipe=style_recipe,
                theme_tokens=theme_tokens,
                theme_fingerprint=brand_fp,
            )
            return fmt
        except Exception as e:
            # Pre-rendering is best-effort — never block FINALIZE on it.
            logger.warning("Eager render of %s for artifact %s failed: %s",
                           fmt, artifact.id, e)
            return None

    def list_available_formats(self, artifact: Artifact) -> dict[str, dict[str, Any]]:
        """Return a ``{format: {file_name, mime_type, size, ...}}`` dict
        of all currently-cached format blobs on the artifact.

        Read directly off the current version's blobs — no
        re-rendering.  Used by the frontend to show "this artifact is
        also available as X" hints.
        """
        version = self._current_version(artifact)
        if not version:
            return {}

        blobs = (
            self.db.query(ArtifactBlob)
            .filter(
                ArtifactBlob.version_id == version.id,
                ArtifactBlob.blob_type == BLOB_TYPE_FORMAT_EXPORT,
            )
            .all()
        )
        out: dict[str, dict[str, Any]] = {}
        for b in blobs:
            fmt = self._format_from_file_name(b.file_name)
            if not fmt:
                continue
            out[fmt] = {
                "blob_id": b.id,
                "file_name": b.file_name,
                "mime_type": b.mime_type,
                "size": b.file_size,
                "checksum": b.checksum,
            }
        return out

    # --- Internals ---------------------------------------------------------

    def _blob_data(self, blob: ArtifactBlob) -> Optional[bytes]:
        """Read blob bytes through the storage abstraction."""
        if blob.data is not None:
            return blob.data
        if not blob.storage_uri:
            return None
        from app.services.artifacts.storage import get_blob_storage
        storage = get_blob_storage(self.db)
        return storage.get(blob.storage_uri)

    def _render_and_store(
        self,
        artifact: Artifact,
        format: str,
        *,
        user_message: str,
        sql: Optional[str],
        source: Optional[str],
        theme: Optional[str] = None,
        mode: Optional[str] = None,
        style_recipe: Optional[str] = None,
        doc_type: Optional[str] = None,
        theme_tokens: Optional[dict] = None,
        theme_fingerprint: Optional[str] = None,
        persist: bool = True,
    ) -> tuple[bytes, str, str]:
        """Render the format, persist as a new ArtifactBlob, return the bytes.

        When ``artifact.canonical_format == "html"`` and the requested format
        is ``docx`` or ``pdf``, we read the original HTML blob and pipe it
        through the HTML→format renderer instead of the normal ReportCardPayload
        pipeline.

        Every render passes through the P0.1 self-healing quality loop:
        semantic audit → deterministic repair → re-audit (up to
        ``ZHANLU_AUDIT_REPAIR_CYCLES`` rounds), so mechanically-fixable
        violations never reach the user.
        """
        # ctx / deck_rows are set by the standard path below and consulted by
        # the post-render copy-polish pass.  They stay None/[] for the
        # canonical-HTML→DOCX/PDF path.
        ctx: Optional[ExportContext] = None
        deck_rows: list[dict[str, Any]] = []

        # ── Canonical HTML → DOCX / PDF path ──
        if (
            artifact.canonical_format == "html"
            and format in ("docx", "pdf")
        ):
            data, mime, ext = self._render_from_html_canonical(artifact, format)
            if not data:
                raise ExportError(
                    f"HTML→{format} renderer returned empty bytes for artifact {artifact.id}"
                )
        else:
            # ── Standard ReportCardPayload path ──
            payload = self._payload_from_artifact(artifact)
            ctx = ExportContext(
                source=source or (payload.source if payload else ""),
                sql=sql,
                conversation_id=artifact.conversation_id,
                user_message=user_message,
            )
            if theme:
                ctx.theme = theme
            ctx.theme_name = theme or ctx.theme
            if mode:
                ctx.mode = mode
            if style_recipe:
                ctx.style_recipe = style_recipe
            if doc_type:
                ctx.doc_type = doc_type
            if theme_tokens:
                ctx.theme_tokens = theme_tokens

            # ── PPT pipeline: planner → router → layout engine ──
            # All flags default OFF, so the report-card renderer below is the
            # unchanged path unless a deployment opts into deck planning.
            if format == "pptx" and settings.PPT_DECK_PLANNER_ENABLED:
                # Data grounding: when opted in, plan the deck off the REAL
                # query rows the agent fetched (ObservationRecords on the
                # artifact's execution / conversation), falling back to the
                # LLM-authored payload.chart.data when nothing is recoverable.
                if settings.PPT_DECK_DATA_GROUNDING_ENABLED:
                    deck_rows = collect_grounded_rows(
                        self.db,
                        artifact=artifact,
                        conversation_id=artifact.conversation_id,
                        execution_id=artifact.execution_id,
                        user_message=user_message,
                    )
                    if not deck_rows:
                        deck_rows = chart_rows(payload) if payload else []
                else:
                    deck_rows = chart_rows(payload) if payload else []
                data, mime, ext, audit_report = self._render_deck_pipeline(
                    payload, ctx, deck_rows,
                    user_message=user_message, artifact=artifact,
                )
                # The dispatcher already ran audit + repair + polish; flag it so
                # we don't double-run the quality loop / polish below.
                deck_pipeline_owned = True
            else:
                data, mime, ext = render(format, payload, ctx)
                deck_pipeline_owned = False
            if not data:
                raise ExportError(f"Renderer returned empty bytes for format {format!r}")

        title = artifact.title or "Zhanlu report"
        file_name = stamp_filename(title, ext)

        # ── P0.1 self-healing quality loop ──
        # When the deck pipeline already owns audit + repair + polish (planner
        # on), reuse its report and skip the duplicate loop.  For every other
        # path (legacy report-card renderer, non-pptx) the loop still runs.
        if deck_pipeline_owned:
            if audit_report is None:
                audit_report = self._run_semantic_audit(format, data)
            audit_history: list[dict[str, Any]] = []
        else:
            data, audit_report, audit_history = self._quality_loop(format, data)

        # Themed one-off renders are returned fresh without caching or
        # audit-record (the per-format cache key doesn't encode the theme yet,
        # so persisting would overwrite the default-theme blob).  The final
        # audit still ran above for observability.
        if not persist:
            if audit_report and audit_report.get("status") == "FAIL":
                logger.warning(
                    "ExportService: themed %s audit FAIL — %s",
                    format, audit_report.get("summary"),
                )
            return data, mime, file_name

        # Persist as a new blob on the current version.
        version = self._current_version(artifact)
        if not version:
            version = self._create_initial_version(artifact)

        blob = self._attach_format_blob(
            artifact=artifact,
            version=version,
            format=format,
            file_name=file_name,
            mime_type=mime,
            data=data,
            theme_fingerprint=theme_fingerprint,
        )

        # P0 enforcement: record the final audit report (+ repair history)
        # on the artifact.  Best-effort by default — a FAIL is logged +
        # recorded but does not block the download unless
        # AUDIT_BLOCK_ON_FAIL is set.
        if audit_report:
            if audit_history:
                audit_report = dict(audit_report)
                audit_report["repair_history"] = [
                    {"status": h.get("status"), "summary": h.get("summary")}
                    for h in audit_history
                ]
            self._record_audit_report(artifact, format, audit_report)
            status = audit_report.get("status")
            if status == "FAIL":
                if AUDIT_BLOCK_ON_FAIL:
                    raise ExportError(
                        f"Semantic audit FAILED for {format} export of artifact "
                        f"{artifact.id}: {audit_report.get('summary')}"
                    )
                logger.warning(
                    "ExportService: semantic audit FAILED (non-blocking) for %s "
                    "artifact %s — %s",
                    format, artifact.id, audit_report.get("summary"),
                )
            elif status == "WARN":
                logger.info(
                    "ExportService: semantic audit WARN for %s artifact %s — %s",
                    format, artifact.id, audit_report.get("summary"),
                )

        # P0.3: render-to-image page thumbnails (best-effort, multi-tenant
        # safe — isolated soffice profile per call).  Doubles as the real
        # preview thumbnails in the artifact panel.
        self._maybe_store_thumbnails(artifact, version, format, data)

        logger.info(
            "ExportService: rendered %s for artifact %s (version %s, %d bytes)",
            format, artifact.id, version.version_number, len(data),
        )
        return data, blob.mime_type, blob.file_name

    # --- PPT deck pipeline helpers -----------------------------------------

    @staticmethod
    def _run_coro(coro: Any) -> Any:
        """Run an async coroutine to completion in any calling context.

        ``_render_and_store`` is synchronous but is reached from both sync
        FastAPI endpoints (threadpool, no running loop) and from inside the
        async agent chat loop (running loop present).  ``asyncio.run`` only
        works in the former; for the latter we run the coroutine in a fresh
        thread with its own event loop.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — safe to drive directly.
            return asyncio.run(coro)

        # A loop is already running (async agent path).  Drive the coroutine
        # in a dedicated thread so we don't deadlock the live loop.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    @staticmethod
    def _resolved_theme_name(ctx: ExportContext) -> str:
        """A validate-able theme name for the deck planner.

        Brand kits supply hex ``theme_tokens`` whose name would NOT pass
        ``validate_theme_name`` — when they're present, hand the planner the
        vendored base name (``ctx.theme``) instead.  Otherwise prefer the
        explicitly resolved ``ctx.theme_name``, then ``ctx.theme``, then the
        ``zhanlu-blue`` default.
        """
        if getattr(ctx, "theme_tokens", None):
            return getattr(ctx, "theme", None) or "zhanlu-blue"
        return (
            getattr(ctx, "theme_name", None)
            or getattr(ctx, "theme", None)
            or "zhanlu-blue"
        )

    def render_pptx_deck(
        self,
        payload: Optional[ReportCardPayload],
        ctx: ExportContext,
        rows: list[dict[str, Any]],
        *,
        user_message: str = "",
        artifact=None,
        user_id: Optional[str] = None,
    ) -> tuple[bytes, str, str]:
        """Public wrapper over the deck pipeline for the artifact-tool path.

        Lets the tool that stores the in-chat artifact render through the SAME
        professional pipeline as the download route, so the in-chat deck and
        the downloaded file are byte-consistent.  Falls back to the legacy
        catalog renderer when the pipeline isn't opted in.
        """
        if not settings.PPT_DECK_PLANNER_ENABLED:
            from app.services.artifacts.exporters.pptx_export import render as _legacy
            return _legacy(payload, ctx)

        data, mime, ext, _audit = self._render_deck_pipeline(
            payload, ctx, rows,
            user_message=user_message, artifact=artifact, user_id=user_id,
        )
        return data, mime, ext

    def _render_deck_pipeline(
        self,
        payload: Optional[ReportCardPayload],
        ctx: ExportContext,
        rows: list[dict[str, Any]],
        *,
        user_message: str,
        artifact=None,
        user_id: Optional[str] = None,
        org_id: str = "default-org",
        app_id: str = "default-app",
    ) -> tuple[bytes, str, str, Optional[dict[str, Any]]]:
        """Run planner → router → layout-engine render for a PPT deck.

        Only reached when ``PPT_DECK_PLANNER_ENABLED`` is on.  The planner
        always returns a plan (LLM when the flag is on, deterministic
        fallback otherwise); the router then classifies it as "structured" vs
        "sandbox".  A full sandbox html2pptx render is an agent-side skill
        concern, so the deterministic export pipeline renders through the
        layout engine as a faithful fallback while recording the route.

        The planner is no longer blind: it receives the resolved theme name,
        the brand ``theme_tokens``, and the user/brand context
        (``build_deck_user_context`` — role / profile / memory / brand kit)
        so decks are themed and personal, not template-filling.

        Returns ``(bytes, mime, ext, audit_report)`` — the audit report comes
        from the shared ``render_dispatcher`` (which already ran the audit +
        deterministic repair + optional polish loop), so the caller does NOT
        re-run the quality loop for this path.
        """
        from app.services.artifacts.deck_planner import build_deck_plan
        from app.services.artifacts.deck_router import (
            classify_profile,
            route_deck,
        )
        from app.services.artifacts.render_dispatcher import render_pptx_from_plan_sync

        # Phase 4: classify the deck profile from the user's intent. The
        # deterministic classifier runs sync (no LLM); an explicit profile
        # carried on the context always wins.
        profile_name = self._run_coro(
            classify_profile(
                user_message,
                explicit=getattr(ctx, "deck_profile", None),
                allow_llm_fallback=False,
            )
        )

        # Personalization + brand: hand the planner the resolved theme name,
        # the brand palette, and the user/brand context so its copy matches
        # the audience and the customer palette instead of a generic template.
        theme_name = self._resolved_theme_name(ctx)
        theme_tokens = getattr(ctx, "theme_tokens", None)
        user_context = build_deck_user_context(
            self.db,
            artifact=artifact,
            user_id=user_id,
            conversation_id=getattr(ctx, "conversation_id", None),
            user_message=user_message,
            org_id=org_id,
            app_id=app_id,
        )

        plan, _profile = self._run_coro(
            build_deck_plan(
                user_message, rows,
                theme_recommendation=theme_name,
                profile_name=profile_name,
                theme_tokens=theme_tokens,
                user_context=user_context,
            )
        )

        ctx.deck_type = plan.deck_type or "data_report"
        ctx.deck_plan = plan

        route = "structured"
        if settings.PPT_SMART_ROUTER_ENABLED:
            route = route_deck(plan, user_message)

        if route == "sandbox":
            # Design-heavy deck.  Try the HTML design path first if opted in;
            # fall back to the structured layout engine (the original behavior)
            # when the new path is disabled or fails.
            if settings.HTML_DESIGN_RENDERER_ENABLED:
                from app.services.artifacts.render_html_deck import (
                    render_html_deck, RenderError, html_design_available,
                )
                if html_design_available():
                    try:
                        data = render_html_deck(plan, ctx)
                        audit_report = {
                            "status": "PASS",
                            "summary": "html_design_renderer",
                            "details": "rendered via Firefox image_fill pipeline",
                        }
                        logger.info(
                            "ExportService: deck rendered via html_design (type=%s)",
                            plan.deck_type,
                        )
                        return (
                            data,
                            "application/vnd.openxmlformats-"
                            "officedocument.presentationml.presentation",
                            "pptx", audit_report,
                        )
                    except RenderError as exc:
                        logger.warning(
                            "ExportService: html_design render failed, "
                            "falling back to layout engine: %s", exc,
                        )
                else:
                    logger.info(
                        "ExportService: HTML_DESIGN_RENDERER_ENABLED but "
                        "browser missing — using layout engine fallback"
                    )
            else:
                # Original (pre-Phase-4) behavior: log and fall through.
                logger.info(
                    "ExportService: deck routed to sandbox (type=%s); "
                    "HTML_DESIGN_RENDERER_ENABLED=False — using layout engine "
                    "as deterministic fallback",
                    plan.deck_type,
                )
                ctx.cover_art = True

        # Render via the shared dispatcher (audit + repair + polish owned there)
        # and capture its report so _render_and_store doesn't re-run the loop.
        data, audit_report = render_pptx_from_plan_sync(plan, rows, ctx)
        return data, "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx", audit_report

    def _run_deck_polish(
        self,
        ctx: ExportContext,
        rows: list[dict[str, Any]],
        *,
        user_message: str,
    ) -> Optional[bytes]:
        """Run the one-shot LLM copy-polish pass, then re-render the deck.

        Returns the re-rendered bytes, or ``None`` when the polish produced no
        change / failed (caller then keeps the pre-polish bytes).
        """
        from app.services.artifacts.copy_polish import polish_deck
        from app.services.artifacts.exporters.pptx_export import render_deck

        plan = ctx.deck_plan
        if plan is None:
            return None

        polished = self._run_coro(polish_deck(plan, rows, user_message))
        if polished is plan:  # identity — polish returned the original unchanged
            return None

        ctx.deck_plan = polished
        new_data, _mime, _ext = render_deck(polished, ctx, rows)
        if not new_data:
            logger.warning(
                "ExportService: polished deck re-render produced empty bytes; "
                "keeping pre-polish output"
            )
            return None
        return new_data

    def _render_from_html_canonical(
        self, artifact: Artifact, format: str
    ) -> tuple[bytes, str, str]:
        """Read the artifact's original HTML blob and convert it to ``format``.

        Returns ``(bytes, mime_type, extension)``.
        """
        # Get the original HTML blob
        from app.services.artifacts.artifact_service import ArtifactService
        svc = ArtifactService(self.db)
        html_blob = svc.get_original_blob(artifact.id)
        if not html_blob:
            raise ExportError(
                f"No original blob found for HTML-canonical artifact {artifact.id}"
            )
        html_bytes = svc.get_blob_data(html_blob)
        if not html_bytes:
            raise ExportError(
                f"Original blob data is empty for artifact {artifact.id}"
            )

        if format == "docx":
            from app.services.artifacts.exporters.html_docx import render_html_to_docx
            result = render_html_to_docx(html_bytes)
            return result, (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ), ".docx"

        elif format == "pdf":
            from app.services.artifacts.exporters.html_pdf import render_html_to_pdf
            result = render_html_to_pdf(html_bytes)
            return result, "application/pdf", ".pdf"

        raise ExportError(
            f"Unsupported format {format!r} for HTML-canonical artifact"
        )

    # --- Semantic audit (P0 enforcement loop) -----------------------------

    @staticmethod
    def _audit_script_for(format: str) -> Optional[Path]:
        """Resolve the standalone audit script for a format, or None.

        The audit scripts are zhanlu-owned QA tooling, kept OUTSIDE the
        skills folder (``backend/app/services/artifacts/audits/``) so a
        skill-swap can never silently disable the P0 semantic audit.
        """
        # service.py is at backend/app/services/artifacts/exporters/service.py
        # parents[1] -> backend/app/services/artifacts  ->  .../audits
        audits_dir = Path(__file__).resolve().parents[1] / "audits"
        mapping = {
            "pptx": audits_dir / "audit_deck.py",
            "docx": audits_dir / "audit_doc.py",
        }
        return mapping.get(format)

    def _run_semantic_audit(
        self, format: str, data: bytes
    ) -> Optional[dict[str, Any]]:
        """Run the format's audit script on the rendered bytes.

        Returns the parsed JSON report, or None if no audit applies, the
        script is absent, or the audit itself errored (never raises — the
        audit is observability infrastructure and must not break a render).
        """
        script = self._audit_script_for(format)
        if script is None or not data or not script.exists():
            return None

        suffix = f".{safe_file_extension(format).lstrip('.')}" if format in ("pptx", "docx") else ".bin"
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                tf.write(data)
                tmp_path = tf.name

            proc = subprocess.run(
                [sys.executable, str(script), tmp_path, "--json"],
                capture_output=True,
                text=True,
                timeout=90,
            )
            # exit 0 = PASS/WARN, exit 1 = FAIL — both emit valid JSON.
            if proc.returncode in (0, 1) and proc.stdout:
                return json.loads(proc.stdout)
            logger.warning(
                "ExportService: audit script %s exited %d — stderr: %s",
                script.name, proc.returncode, (proc.stderr or "")[:500],
            )
            return None
        except subprocess.TimeoutExpired:
            logger.warning("ExportService: audit script %s timed out", script.name)
            return None
        except Exception as e:
            logger.warning("ExportService: semantic audit for %s failed: %s", format, e)
            return None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # --- P0.1 self-healing quality loop -----------------------------------

    def _quality_loop(
        self, format: str, data: bytes
    ) -> tuple[bytes, Optional[dict[str, Any]], list[dict[str, Any]]]:
        """Run audit → deterministic repair → re-audit until PASS or budget out.

        Returns ``(final_bytes, final_report, history)`` where ``history``
        holds the superseded FAIL reports (one per repaired cycle).  Never
        raises — any repair/audit error leaves the original bytes intact.
        """
        report = self._run_semantic_audit(format, data)
        history: list[dict[str, Any]] = []
        cycles_left = _repair_cycles()
        while (
            report
            and report.get("status") == "FAIL"
            and cycles_left > 0
        ):
            from app.services.artifacts.repairs import repair_artifact

            repaired = repair_artifact(format, data, report)
            if not repaired:
                break  # nothing mechanically fixable — stop the loop
            history.append(report)
            data = repaired
            cycles_left -= 1
            logger.info(
                "ExportService: applied deterministic repair for %s (cycle %d)",
                format, len(history),
            )
            report = self._run_semantic_audit(format, data)
        return data, report, history

    # --- Multi-tenant brand kit (P1.2) -------------------------------------

    def _resolve_brand_tokens(
        self,
        artifact: Artifact,
        *,
        org_id: Optional[str],
        app_id: Optional[str],
    ) -> tuple[Optional[dict], Optional[str]]:
        """Resolve the workspace brand kit to (theme_tokens, fingerprint).

        Returns (None, None) when no kit is configured — callers then use
        the default zhanlu-blue theme and the legacy cache behavior.
        """
        org = org_id or getattr(artifact, "org_id", None) or "default-org"
        app = app_id or getattr(artifact, "app_id", None) or "default-app"
        try:
            from app.services.artifacts.brand_kit import (
                brand_kit_to_theme_tokens,
                get_brand_kit,
            )

            kit = get_brand_kit(self.db, org_id=org, app_id=app)
            if not kit:
                return None, None
            tokens = brand_kit_to_theme_tokens(kit)
            import hashlib as _hashlib

            fp = _hashlib.sha256(
                json.dumps(tokens, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            return tokens, fp
        except Exception as e:
            logger.warning("ExportService: brand kit resolution failed: %s", e)
            return None, None

    def _cached_fingerprint(self, artifact: Artifact, format: str) -> Optional[str]:
        """The theme fingerprint recorded on the cached payload_formats entry."""
        meta = artifact.metadata_json or {}
        entry = (meta.get("payload_formats") or {}).get(format) or {}
        return entry.get("theme_fingerprint")

    # --- P0.3 render-to-image thumbnails -----------------------------------

    def _maybe_store_thumbnails(
        self,
        artifact: Artifact,
        version: ArtifactVersion,
        format: str,
        data: bytes,
    ) -> None:
        """Render per-page PNG thumbnails and store them on the version.

        Best-effort: any failure is logged and swallowed so thumbnails can
        never break an export.  Thumbnails are stored as
        ``blob_type="thumbnail"`` blobs named ``<stem>.page-NN.png`` and the
        format's ``payload_formats`` entry gains a ``thumbnails`` list the
        frontend can use for a slide/page strip in the artifact panel.
        """
        try:
            from app.services.artifacts.thumbnails import render_page_thumbnails

            pages = render_page_thumbnails(format, data)
            if not pages:
                return

            from app.services.artifacts.artifact_service import ArtifactService

            svc = ArtifactService(self.db)
            stem = (artifact.title or "artifact").rsplit(".", 1)[0][:40] or "artifact"
            blob_ids: list[str] = []
            for i, png in enumerate(pages, start=1):
                tb = svc.store_blob(
                    version_id=version.id,
                    blob_type="thumbnail",
                    file_name=f"{stem}.page-{i:02d}.png",
                    mime_type="image/png",
                    data=png,
                )
                blob_ids.append(tb.id)

            meta = dict(artifact.metadata_json or {})
            formats = dict(meta.get("payload_formats") or {})
            entry = dict(formats.get(format) or {})
            entry["thumbnails"] = blob_ids
            entry["thumbnail_count"] = len(blob_ids)
            formats[format] = entry
            meta["payload_formats"] = formats
            artifact.metadata_json = meta
            self.db.commit()
            logger.info(
                "ExportService: stored %d page thumbnail(s) for %s artifact %s",
                len(blob_ids), format, artifact.id,
            )
        except Exception as e:
            logger.warning(
                "ExportService: thumbnail generation failed for %s artifact %s: %s",
                format, artifact.id, e,
            )

    def _record_audit_report(
        self, artifact: Artifact, format: str, report: dict[str, Any]
    ) -> None:
        """Persist the audit report under metadata_json['audit_reports'][format]."""
        meta = dict(artifact.metadata_json or {})
        audits = dict(meta.get("audit_reports") or {})
        audits[format] = report
        meta["audit_reports"] = audits
        artifact.metadata_json = meta
        self.db.commit()

    def _attach_format_blob(
        self,
        *,
        artifact: Artifact,
        version: ArtifactVersion,
        format: str,
        file_name: str,
        mime_type: str,
        data: bytes,
        theme_fingerprint: Optional[str] = None,
    ) -> ArtifactBlob:
        """Store the rendered bytes as a new ArtifactBlob + update
        ``metadata_json['payload_formats'][format]`` so the router can
        find it later without re-rendering.
        """
        from app.services.artifacts.artifact_service import ArtifactService

        svc = ArtifactService(self.db)
        blob = svc.store_blob(
            version_id=version.id,
            blob_type=BLOB_TYPE_FORMAT_EXPORT,
            file_name=file_name,
            mime_type=mime_type,
            data=data,
        )

        # Update the artifact's metadata_json["payload_formats"]
        meta = dict(artifact.metadata_json or {})
        formats = dict(meta.get("payload_formats") or {})
        entry = {
            "blob_id": blob.id,
            "version_id": version.id,
            "file_name": file_name,
            "mime_type": mime_type,
            "size": len(data),
            "checksum": blob.checksum,
        }
        if theme_fingerprint:
            entry["theme_fingerprint"] = theme_fingerprint
        formats[format] = entry
        meta["payload_formats"] = formats
        artifact.metadata_json = meta
        self.db.commit()
        self.db.refresh(artifact)
        return blob

    def _find_cached_format_blob(
        self, artifact: Artifact, format: str
    ) -> Optional[ArtifactBlob]:
        """Return the cached format blob for ``format`` on the artifact's
        current version, or None if it doesn't exist.
        """
        version = self._current_version(artifact)
        if not version:
            return None
        blobs = (
            self.db.query(ArtifactBlob)
            .filter(
                ArtifactBlob.version_id == version.id,
                ArtifactBlob.blob_type == BLOB_TYPE_FORMAT_EXPORT,
            )
            .all()
        )
        for b in blobs:
            if self._format_from_file_name(b.file_name) == format:
                return b
        return None

    def _current_version(self, artifact: Artifact) -> Optional[ArtifactVersion]:
        if not artifact.current_version_id:
            return None
        return (
            self.db.query(ArtifactVersion)
            .filter(ArtifactVersion.id == artifact.current_version_id)
            .first()
        )

    def _create_initial_version(self, artifact: Artifact) -> ArtifactVersion:
        """Create a fresh v1 version on an artifact that doesn't have one yet."""
        version = ArtifactVersion(
            id=_new_uuid(),
            artifact_id=artifact.id,
            version_number=1,
            status="preview_ready",
        )
        self.db.add(version)
        artifact.current_version_id = version.id
        self.db.commit()
        self.db.refresh(version)
        return version

    def _payload_from_artifact(self, artifact: Artifact) -> ReportCardPayload:
        """Reconstruct a `ReportCardPayload` from the artifact's metadata.

        The Synexia FINALIZE step stores the full payload in
        ``metadata_json['report_card_payload']`` — we reuse it on
        every render.  This means we don't need to plumb the payload
        all the way from the chat loop into the download route.
        """
        from app.services.synexia.contracts import ReportCardPayload

        meta = artifact.metadata_json or {}
        raw = meta.get("report_card_payload")
        if isinstance(raw, dict):
            try:
                return ReportCardPayload.model_validate(raw)
            except Exception as e:
                logger.warning(
                    "ExportService: stored report_card_payload did not validate "
                    "for artifact %s: %s — falling back to minimal payload",
                    artifact.id, e,
                )

        # Fallback: synthesize a minimal payload from the artifact's
        # own fields so the renderers still produce *something*.
        return ReportCardPayload(
            title=artifact.title or "Zhanlu report",
            source=(meta.get("source") or ""),
            summary=artifact.description or "",
            kpis=[],
            insights=[],
            chart=None,
            user_signal=meta.get("user_signal", "default"),
        )

    def _pick_default_format(
        self, *, user_signal: str, user_message: str
    ) -> Optional[str]:
        """Decide which format to eager-render at finalize time.

        Returns None if the user_signal doesn't suggest a download at
        all (e.g. user just wants to read the in-chat card).
        """
        signal = (user_signal or "default").lower()
        if signal not in DEFAULT_FORMAT_BY_SIGNAL:
            return None

        # If the user message mentions a specific format, honor that.
        msg = (user_message or "").lower()
        for fmt, keywords in _FORMAT_HINT_KEYWORDS:
            for kw in keywords:
                if kw in msg:
                    return fmt
        return DEFAULT_FORMAT_BY_SIGNAL[signal]

    @staticmethod
    def _format_from_file_name(file_name: str) -> Optional[str]:
        if not file_name:
            return None
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        for fmt in SUPPORTED_FORMATS:
            if safe_file_extension(fmt).lstrip(".") == ext:
                return fmt
        return None


def _new_uuid() -> str:
    """Local helper to avoid importing uuid at the top of every test fixture."""
    import uuid
    return str(uuid.uuid4())


__all__ = [
    "ExportService",
    "ExportError",
    "BLOB_TYPE_FORMAT_EXPORT",
    "DEFAULT_FORMAT_BY_SIGNAL",
]
