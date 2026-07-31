"""HTML → PDF renderer — converts standalone HTML bytes to a PDF file.

Two-stage strategy:
1. **weasyprint** (preferred) — pure-Python, excellent CSS support, no
   external daemon needed.
2. **LibreOffice headless** (fallback) — converts the HTML via ``--convert-to pdf``.
   Requires ``libreoffice`` (or ``soffice``) on PATH.

Public entry point: ``render_html_to_pdf(html_bytes) -> bytes``

**Disk-write audit (2026-07-15):**  The weasyprint path is fully in-memory
(no disk I/O).  The LibreOffice fallback writes the input HTML to a
``TemporaryDirectory`` (auto-cleaned on context-manager exit) because
LibreOffice requires a real file.  The temp directory is cleaned up
automatically.  No persistent disk writes remain.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

MIME = "application/pdf"
EXT = ".pdf"

_LIBREOFFICE_PATH = shutil.which("libreoffice") or shutil.which("soffice")


def render_html_to_pdf(html_bytes: bytes) -> bytes:
    """Convert HTML content to a PDF file.

    Tries weasyprint first; falls back to LibreOffice headless if
    weasyprint is unavailable or fails.
    """
    # Try weasyprint first — pure Python, best CSS support
    try:
        return _via_weasyprint(html_bytes)
    except (ImportError, Exception) as exc:
        logger.info("weasyprint HTML→PDF failed (%s), trying LibreOffice fallback", exc)

    # Fallback: LibreOffice headless
    try:
        return _via_libreoffice(html_bytes)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        logger.error("LibreOffice HTML→PDF fallback failed: %s", exc)
        raise RuntimeError(
            "Cannot render HTML to PDF — missing both weasyprint and LibreOffice"
        ) from exc
    except Exception as exc:
        logger.error("HTML→PDF render failed: %s", exc)
        raise


# ── weasyprint path ────────────────────────────────────────────────────────


def _via_weasyprint(html_bytes: bytes) -> bytes:
    """Render HTML to PDF using weasyprint."""
    from weasyprint import HTML

    # weasyprint can read from a string or bytes
    html_str = html_bytes.decode("utf-8", errors="replace")
    pdf_bytes = HTML(string=html_str).write_pdf()
    logger.info("HTML→PDF via weasyprint: %d bytes", len(pdf_bytes))
    return pdf_bytes


# ── LibreOffice fallback ───────────────────────────────────────────────────


def _via_libreoffice(html_bytes: bytes) -> bytes:
    """Convert HTML to PDF using LibreOffice headless."""
    if not _LIBREOFFICE_PATH:
        raise FileNotFoundError(
            "LibreOffice (libreoffice/soffice) not found on PATH"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, "input.html")
        with open(input_path, "wb") as f:
            f.write(html_bytes)

        result = subprocess.run(
            [
                _LIBREOFFICE_PATH,
                "--headless",
                "--convert-to", "pdf",
                "--outdir", tmpdir,
                input_path,
            ],
            capture_output=True,
            timeout=60,
        )

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )

        pdf_path = os.path.join(tmpdir, "input.pdf")
        if not os.path.exists(pdf_path):
            raise RuntimeError("LibreOffice produced no PDF output")

        with open(pdf_path, "rb") as f:
            pdf_data = f.read()

        logger.info("HTML→PDF via LibreOffice: %d bytes", len(pdf_data))
        return pdf_data


def render(payload, ctx=None):
    """Adapter: ``render(payload, ctx) -> (bytes, mime, ext)`` so this module
    can be registered as an ExportService renderer alongside existing ones.

    ``payload`` is expected to be raw HTML bytes (not a ReportCardPayload).
    """
    if isinstance(payload, bytes):
        html = payload
    elif isinstance(payload, str):
        html = payload.encode("utf-8")
    else:
        raise TypeError(f"html_pdf render expects bytes or str, got {type(payload)}")
    return render_html_to_pdf(html), MIME, EXT
