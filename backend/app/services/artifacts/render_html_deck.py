"""Top-level entry point for the HTML design renderer.

``render_html_deck(plan, ctx)`` takes a planner-produced ``DeckPlan``
and produces PPTX bytes using the HTML design path:

  1. Resolve theme (select_theme)
  2. Render each SlidePlan as HTML (render_slide)
  3. Wrap into a printable index.html (build_stage)
  4. Convert HTML → PDF → PNG → PPTX image fill (render_image_fill)
"""
from __future__ import annotations

import logging
from typing import List

from app.services.synexia.contracts import DeckPlan
from app.services.artifacts.exporters._common import ExportContext
from app.services.artifacts.themes import select_theme, ThemePreset
from app.services.artifacts.html_slide_generator import render_slide, build_stage
from app.services.artifacts.html_to_pptx import (
    render_image_fill, image_fill_available, PptxRenderError,
)

logger = logging.getLogger(__name__)


class RenderError(Exception):
    """Raised by render_html_deck when the HTML path fails.

    Caller (export service) catches this and falls back to the
    structured python-pptx renderer.
    """


def html_design_available() -> bool:
    """True when the image_fill pipeline can run (soffice + pdftoppm present)."""
    return image_fill_available()


def render_html_deck(plan: DeckPlan, ctx: ExportContext) -> bytes:
    """Render a DeckPlan as PPTX using the HTML design path.

    Raises ``RenderError`` on any failure.  The caller (export service)
    is expected to fall back to the structured renderer.
    """
    if not html_design_available():
        raise RenderError(
            "HTML design renderer unavailable: soffice or pdftoppm missing"
        )

    user_message = getattr(ctx, "user_message", "") or ""
    theme: ThemePreset = select_theme(plan, user_message)

    # 1. Render each slide; skip unknown layouts
    slide_htmls: List[str] = []
    for slide in plan.slides:
        try:
            slide_htmls.append(render_slide(slide.layout, slide, theme))
        except NotImplementedError as exc:
            logger.warning(
                "render_html_deck: layout %r not implemented, skipping: %s",
                slide.layout, exc,
            )
            continue

    if not slide_htmls:
        raise RenderError("no slides could be rendered as HTML")

    # 2. Build printable index.html
    stage_html = build_stage(slide_htmls)

    # 3. Convert to PPTX (image fill)
    try:
        return render_image_fill(stage_html)
    except PptxRenderError as exc:
        raise RenderError(f"image_fill pipeline failed: {exc}") from exc


__all__ = ["render_html_deck", "RenderError", "html_design_available"]
