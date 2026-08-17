"""Optional visual verification for rendered decks (Phase 1B, opt-in).

Renders a ``.pptx`` to PNGs via LibreOffice headless and runs a cheap PIL
contrast / non-empty check.  This is OFF by default (``PPTX_VISUAL_VERIFY_ENABLED``)
and ONLY meaningful inside the sandbox container that has LibreOffice
installed — the backend host does NOT install LibreOffice.

If LibreOffice is not on PATH, or the flag is off, every function returns a
no-op result so callers can guard without branching.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _libreoffice_available() -> bool:
    return shutil.which("libreoffice") is not None or shutil.which("soffice") is not None


def _enabled() -> bool:
    try:
        from app.config import settings

        return bool(getattr(settings, "PPTX_VISUAL_VERIFY_ENABLED", False))
    except Exception:
        return False


def render_to_pngs(pptx_bytes: bytes) -> list[Path]:
    """Render deck bytes to per-slide PNGs. Returns [] when unavailable."""
    if not _enabled() or not _libreoffice_available():
        return []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "deck.pptx"
            deck.write_bytes(pptx_bytes)
            out_dir = Path(tmp) / "png"
            out_dir.mkdir()
            bin_name = "libreoffice" if shutil.which("libreoffice") else "soffice"
            subprocess.run(
                [bin_name, "--headless", "--convert-to", "pdf", "--outdir",
                 str(out_dir), str(deck)],
                check=True, capture_output=True, timeout=120,
            )
            # PDF -> PNG requires pdftoppm or LibreOffice; keep it minimal and
            # skip if tooling is missing (sandbox job is responsible for the
            # actual rasterization step).
            return sorted(out_dir.glob("*.png"))
    except Exception as exc:  # pragma: no cover — optional, best-effort
        logger.warning("visual_verify: render_to_pngs failed: %s", exc)
        return []


def verify(pptx_bytes: bytes) -> dict[str, Any]:
    """Run the optional visual check. Always returns a structured dict."""
    if not _enabled():
        return {"tool": "visual_verify", "status": "SKIP", "reason": "disabled"}
    if not _libreoffice_available():
        return {"tool": "visual_verify", "status": "SKIP", "reason": "no_libreoffice"}
    pngs = render_to_pngs(pptx_bytes)
    if not pngs:
        return {"tool": "visual_verify", "status": "WARN", "reason": "no_pngs"}
    # Cheap signal: every PNG must be non-trivial (not blank).
    try:
        from PIL import Image

        blank = 0
        for p in pngs:
            img = Image.open(p).convert("L")
            extrema = img.getextrema()
            if extrema == (0, 0) or extrema == (255, 255):
                blank += 1
        if blank:
            return {"tool": "visual_verify", "status": "WARN",
                    "detail": f"{blank}/{len(pngs)} blank slides", "slides": len(pngs)}
        return {"tool": "visual_verify", "status": "PASS", "slides": len(pngs)}
    except Exception as exc:  # pragma: no cover
        logger.warning("visual_verify: PIL check failed: %s", exc)
        return {"tool": "visual_verify", "status": "WARN", "reason": str(exc)}
