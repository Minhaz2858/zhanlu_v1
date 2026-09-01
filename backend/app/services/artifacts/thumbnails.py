"""Render-to-image thumbnails for exported artifacts (PPTX / DOCX).

Pipeline: ``soffice --headless --convert-to pdf`` → ``pdftoppm -png``
→ one PNG per page/slide (capped).  Everything runs in a per-call
temporary directory that is always cleaned up, and soffice gets an
**isolated user profile** (``-env:UserInstallation=...``) so concurrent
renders from different users/requests never fight over the global
LibreOffice profile lock — the classic multi-tenant soffice failure
mode.

The module is deliberately best-effort: any missing binary, timeout, or
conversion error returns ``[]`` instead of raising, so thumbnails can
never break an export.

Config (env):
* ``ZHANLU_THUMBNAILS_ENABLED`` — "0" disables entirely (default "1")
* ``ZHANLU_THUMBNAILS_MAX_PAGES`` — cap on rendered pages (default 12)
* ``ZHANLU_THUMBNAILS_DPI`` — render DPI for pdftoppm (default 96)
* ``ZHANLU_THUMBNAILS_TIMEOUT`` — per-subprocess timeout seconds (default 120)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("ZHANLU_THUMBNAILS_ENABLED", "1") != "0"


def _max_pages() -> int:
    try:
        return max(1, int(os.environ.get("ZHANLU_THUMBNAILS_MAX_PAGES", "12")))
    except ValueError:
        return 12


def _dpi() -> int:
    try:
        return max(50, int(os.environ.get("ZHANLU_THUMBNAILS_DPI", "96")))
    except ValueError:
        return 96


def _timeout() -> int:
    try:
        return max(10, int(os.environ.get("ZHANLU_THUMBNAILS_TIMEOUT", "120")))
    except ValueError:
        return 120


def _find_binary(names: tuple[str, ...]) -> str | None:
    for n in names:
        path = shutil.which(n)
        if path:
            return path
    return None


def thumbnails_available() -> bool:
    """True when the external binaries needed for thumbnails are present."""
    return _enabled() and _find_binary(("soffice", "libreoffice")) is not None


def render_page_thumbnails(format: str, data: bytes) -> list[bytes]:
    """Render ``data`` (pptx/docx bytes) to a list of per-page PNG bytes.

    Returns [] on any failure (missing tools, timeout, bad input) — this
    is observability/UX infrastructure and must never raise into the
    export path.
    """
    fmt = (format or "").lower().strip()
    if fmt not in ("pptx", "docx") or not data or not _enabled():
        return []

    soffice = _find_binary(("soffice", "libreoffice"))
    if not soffice:
        logger.debug("thumbnails: soffice not found — skipping")
        return []

    pdftoppm = _find_binary(("pdftoppm",))

    workdir = tempfile.mkdtemp(prefix="zhanlu-thumbs-")
    try:
        src = Path(workdir) / f"input.{fmt}"
        src.write_bytes(data)

        # Isolated LibreOffice profile per call — REQUIRED for concurrent
        # multi-user renders (the default profile is a single-instance lock).
        profile = Path(workdir) / "lo-profile"
        profile.mkdir(parents=True, exist_ok=True)

        try:
            proc = subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation=file://{profile}",
                    "--headless", "--norestore", "--nolockcheck", "--nologo",
                    "--convert-to", "pdf",
                    "--outdir", workdir,
                    str(src),
                ],
                capture_output=True,
                text=True,
                timeout=_timeout(),
            )
        except subprocess.TimeoutExpired:
            logger.warning("thumbnails: soffice timed out after %ss", _timeout())
            return []

        pdf_path = src.with_suffix(".pdf")
        if proc.returncode != 0 or not pdf_path.exists():
            logger.warning(
                "thumbnails: soffice exit %d — %s",
                proc.returncode, (proc.stderr or proc.stdout or "")[:300],
            )
            return []

        # Preferred path: pdftoppm → one PNG per page.
        if pdftoppm:
            prefix = str(Path(workdir) / "page")
            try:
                ppm = subprocess.run(
                    [
                        pdftoppm, "-png", "-r", str(_dpi()),
                        "-l", str(_max_pages()),
                        str(pdf_path), prefix,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=_timeout(),
                )
            except subprocess.TimeoutExpired:
                logger.warning("thumbnails: pdftoppm timed out")
                return []
            if ppm.returncode != 0:
                logger.warning("thumbnails: pdftoppm exit %d — %s",
                               ppm.returncode, (ppm.stderr or "")[:300])
                return []
            pages = sorted(Path(workdir).glob("page-*.png"))
            return [p.read_bytes() for p in pages if p.stat().st_size > 0]

        # Fallback: no pdftoppm → return the PDF as a single "preview"
        # payload.  Callers key off mime type, so we signal by wrapping in
        # an empty list here and letting the caller render the PDF blob
        # itself.  (Kept simple: no PDF passthrough for now.)
        logger.debug("thumbnails: pdftoppm not found — skipping PNG extraction")
        return []
    except Exception as e:
        logger.warning("thumbnails: render failed for %s: %s", fmt, e)
        return []
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


__all__ = ["render_page_thumbnails", "thumbnails_available"]
