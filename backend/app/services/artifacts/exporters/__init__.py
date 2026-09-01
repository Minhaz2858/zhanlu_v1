"""Artifact export skills — render a `ReportCardPayload` into a downloadable file.

This package is the v1 of the "artifact-export skills" (Task 6).  It exposes
one function per supported output format — all of them are pure, deterministic,
side-effect-free renderers that take a Pydantic `ReportCardPayload` plus a
small `ExportContext` (source label, SQL, conversation id) and return
``(bytes, mime_type, file_extension)``.

The functions are intentionally NOT coupled to the DB / ArtifactService:
that layer (orchestration, caching, persistence) is the responsibility of
``app.services.artifacts.exporters.ExportService``, which lives in
``service.py`` in this package.

Adding a new format means:
  1. Add a new module (``<fmt>_export.py``) with a ``render(payload, ctx)``
     function.
  2. Register it in ``SUPPORTED_FORMATS`` below.

Public surface:

* ``render(format, payload, ctx)`` — the only function callers need.
* ``SUPPORTED_FORMATS`` — supported format registry, used by tests and the router.
* ``ExportContext`` — typed input (source label, SQL, conversation id).
* ``safe_file_extension`` / ``safe_mime_type`` — small string utilities.
"""

from __future__ import annotations

from typing import Optional

from app.services.synexia.contracts import ReportCardPayload

# ExportContext lives in `_common` (it's the only thing every renderer
# imports, and re-exporting it from here lets callers do
# `from app.services.artifacts.exporters import ExportContext`).
from app.services.artifacts.exporters._common import ExportContext


# --- Format registry ---------------------------------------------------------
# Maps the on-the-wire format string (what the frontend sends as
# ?format=... and what gets stored in metadata_json["payload_formats"])
# to the renderer module that produces the bytes.

SUPPORTED_FORMATS = ("pdf", "pptx", "xlsx", "csv", "docx", "html")


# Sentinel for "format unknown".  Returned as ``(b"", "application/octet-stream", "")``
# so callers can detect it without an exception.
_UNKNOWN = (b"", "application/octet-stream", "")


def render(format: str, payload: ReportCardPayload, ctx: Optional[ExportContext] = None):
    """Render `payload` into the requested format.

    Returns a tuple ``(bytes, mime_type, file_extension)``.

    Unknown format -> ``(b"", "application/octet-stream", "")``.  Callers
    that need to distinguish "format unknown" from "format produced empty
    output" can check ``file_extension == ""``.
    """
    fmt = (format or "").lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        return _UNKNOWN

    ctx = ctx or ExportContext()

    if fmt == "pdf":
        from app.services.artifacts.exporters.pdf_export import render as _render
        return _render(payload, ctx)
    if fmt == "pptx":
        from app.services.artifacts.exporters.pptx_export import render as _render
        return _render(payload, ctx)
    if fmt == "xlsx":
        from app.services.artifacts.exporters.xlsx_export import render as _render
        return _render(payload, ctx)
    if fmt == "csv":
        from app.services.artifacts.exporters.csv_export import render as _render
        return _render(payload, ctx)
    if fmt == "docx":
        from app.services.artifacts.exporters.docx_export import render as _render
        return _render(payload, ctx)
    if fmt == "html":
        # HTML is the canonical source — return as-is (identity renderer).
        # Callers that need HTML→DOCX/PDF use render_html_to_*() directly.
        if isinstance(payload, bytes):
            return payload, "text/html; charset=utf-8", ".html"
        if isinstance(payload, str):
            return payload.encode("utf-8"), "text/html; charset=utf-8", ".html"
        # Fallback: wrap ReportCardPayload as HTML
        from app.services.artifacts.exporters.docx_export import _build_html
        html_str = _build_html(payload)
        return html_str.encode("utf-8"), "text/html; charset=utf-8", ".html"

    return _UNKNOWN  # pragma: no cover — covered by SUPPORTED_FORMATS check


def safe_file_extension(format: str) -> str:
    """Map a format string to a safe file extension (with leading dot)."""
    fmt = (format or "").lower().strip()
    return {
        "pdf": ".pdf",
        "pptx": ".pptx",
        "xlsx": ".xlsx",
        "csv": ".csv",
        "docx": ".docx",
        "html": ".html",
    }.get(fmt, "")


def safe_mime_type(format: str) -> str:
    """Map a format string to its canonical MIME type."""
    fmt = (format or "").lower().strip()
    return {
        "pdf": "application/pdf",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "csv": "text/csv; charset=utf-8",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "html": "text/html; charset=utf-8",
    }.get(fmt, "application/octet-stream")


__all__ = [
    "ExportContext",
    "SUPPORTED_FORMATS",
    "render",
    "safe_file_extension",
    "safe_mime_type",
]
