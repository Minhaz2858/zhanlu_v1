"""HTML → PPTX converters.

* ``render_image_fill(stage_html)`` — full-fidelity static image fills.
  v1.0 default.  Pipeline: stage_html → ``soffice --headless
  --convert-to pdf`` → ``pdftoppm -png -r 192`` → python-pptx with
  each PNG as a 16:9 image fill.  Reuses the soffice + pdftoppm
  pattern from ``backend/app/services/artifacts/thumbnails.py``.

* ``render_editable_text(stage_html)`` — text-editable native PPTX.
  v1.1 (not implemented in this plan; signature reserved).
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from pptx import Presentation
from pptx.util import Inches

logger = logging.getLogger(__name__)


class PptxRenderError(Exception):
    """Raised when the image-fill pipeline cannot produce a PPTX."""


def _find_binary(names: Tuple[str, ...]) -> Optional[str]:
    for n in names:
        path = shutil.which(n)
        if path:
            return path
    return None


def image_fill_available() -> bool:
    """True when ``soffice`` and ``pdftoppm`` are installed and on PATH."""
    return (
        _find_binary(("soffice", "libreoffice")) is not None
        and _find_binary(("pdftoppm",)) is not None
    )


def _convert_html_to_pdf(stage_html: str, workdir: Path) -> Path:
    soffice = _find_binary(("soffice", "libreoffice"))
    if soffice is None:
        raise PptxRenderError("soffice/libreoffice not found on PATH")

    html_path = workdir / "index.html"
    html_path.write_text(stage_html, encoding="utf-8")

    profile_dir = workdir / "soffice_profile"
    profile_dir.mkdir(exist_ok=True)

    cmd = [
        soffice, "--headless", "--norestore", "--nologo",
        "--nofirststartwizard",
        f"-env:UserInstallation=file://{profile_dir}",
        "--convert-to", "pdf", "--outdir", str(workdir),
        str(html_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=60, cwd=str(workdir),
        )
    except subprocess.TimeoutExpired as exc:
        raise PptxRenderError("soffice timeout after 60s") from exc

    if proc.returncode != 0:
        raise PptxRenderError(
            f"soffice exited {proc.returncode}: {proc.stderr[:500]}"
        )

    pdf_path = workdir / "index.pdf"
    if not pdf_path.exists():
        raise PptxRenderError(f"soffice produced no PDF (stderr: {proc.stderr[:500]})")
    return pdf_path


def _convert_pdf_to_pngs(pdf_path: Path, workdir: Path) -> List[Path]:
    pdftoppm = _find_binary(("pdftoppm",))
    if pdftoppm is None:
        raise PptxRenderError("pdftoppm not found on PATH")

    prefix = workdir / "slide"
    cmd = [pdftoppm, "-png", "-r", "192", str(pdf_path), str(prefix)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, cwd=str(workdir),
        )
    except subprocess.TimeoutExpired as exc:
        raise PptxRenderError("pdftoppm timeout after 60s") from exc

    if proc.returncode != 0:
        raise PptxRenderError(
            f"pdftoppm exited {proc.returncode}: {proc.stderr[:500]}"
        )

    pngs = sorted(workdir.glob("slide-*.png"))
    if not pngs:
        raise PptxRenderError("pdftoppm produced no PNGs")
    return pngs


def _build_pptx_from_pngs(pngs: List[Path]) -> bytes:
    pres = Presentation()
    pres.slide_width = Inches(13.333)  # 16:9
    pres.slide_height = Inches(7.5)
    blank_layout = pres.slide_layouts[6]

    for png in pngs:
        slide = pres.slides.add_slide(blank_layout)
        slide.shapes.add_picture(
            str(png), 0, 0,
            width=pres.slide_width, height=pres.slide_height,
        )

    buf = io.BytesIO()
    pres.save(buf)
    return buf.getvalue()


def render_image_fill(stage_html: str) -> bytes:
    """Convert a stage HTML to PPTX bytes (image-fill pipeline).

    Raises ``PptxRenderError`` on any failure.
    """
    if not image_fill_available():
        raise PptxRenderError(
            "image_fill pipeline unavailable: soffice or pdftoppm missing"
        )

    with tempfile.TemporaryDirectory(prefix="zhanlu_html2pptx_") as td:
        workdir = Path(td)
        try:
            pdf_path = _convert_html_to_pdf(stage_html, workdir)
            pngs = _convert_pdf_to_pngs(pdf_path, workdir)
            pptx_bytes = _build_pptx_from_pngs(pngs)
        except PptxRenderError:
            raise
        except Exception as exc:
            raise PptxRenderError(f"image_fill pipeline failed: {exc}") from exc

    if not pptx_bytes or not zipfile.is_zipfile(io.BytesIO(pptx_bytes)):
        raise PptxRenderError("produced output is not a valid PPTX (zip)")
    return pptx_bytes


__all__ = ["render_image_fill", "image_fill_available", "PptxRenderError"]
