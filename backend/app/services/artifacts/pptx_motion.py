"""Slide transition injection for python-pptx decks.

python-pptx has no transition API, but a fade transition is a tiny XML
addition on each slide: ``<p:transition spd="med"><p:fade/></p:transition>``
inserted as a child of the ``<p:sld>`` element. PowerPoint, LibreOffice and
WPS all honor it. Wrapped so ANY failure is swallowed — a transition must
never break a render.

Gated by ``settings.DECK_TRANSITIONS_ENABLED`` (default True).
"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)

_TRANSITION_XML = (
    '<p:transition xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'spd="med"><p:fade/></p:transition>'
)


def add_fade_transitions(prs) -> None:
    """Inject a medium fade transition into every slide of ``prs``.

    Pure additive XML — runs before save, never touches shapes/text. On any
    unexpected failure logs and continues (deck still renders).
    """
    if not settings.DECK_TRANSITIONS_ENABLED:
        return
    try:
        from lxml import etree
    except Exception:  # pragma: no cover — lxml ships with python-pptx
        return
    try:
        for slide in prs.slides:
            sld = slide._element  # CT_Slide
            # Skip if a transition already exists.
            if sld.find(
                "{http://schemas.openxmlformats.org/presentationml/2006/main}transition"
            ) is not None:
                continue
            el = etree.fromstring(_TRANSITION_XML)
            sld.append(el)
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        logger.warning("add_fade_transitions failed (deck still renders): %s", exc)


__all__ = ["add_fade_transitions"]
