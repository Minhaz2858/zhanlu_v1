"""Marker contract parser for ◤FMT◤{...}◤END_FMT◤ blocks in assistant text.

Skills like `docx`, `pptx`, and `artifacts-builder` instruct the LLM to
emit a marker at the end of its reply describing the file it just wrote to
`outputs/`. This module extracts those markers so the backend can route
them into `create_artifact`.

The marker shape is:

    ◤MD_DOCX◤{"md_path": "outputs/report.md", "filename": "Report.docx"}◤END_MD_DOCX◤
    ◤HTML_DOCX◤{"html_path": "outputs/r.html", "filename": "R.docx"}◤END_HTML_DOCX◤
    ◤PPTX◤{"slides_path": "outputs/deck.json", "filename": "Deck.pptx"}◤END_PPTX◤
    ◤DASHBOARD◤{"html_path": "outputs/dash.html", "filename": "Dash.html", "title": "..."}◤END_DASHBOARD◤

Supported kinds (extensible):
- MD_DOCX    — markdown file → DOCX (pandoc pipeline)
- HTML_DOCX  — HTML file → DOCX (preserves styling)
- PPTX       — slide-spec JSON → PPTX (python-pptx pipeline)
- DASHBOARD  — self-contained interactive HTML dashboard → html artifact
              (Chart.js / KPI cards / filters / sortable tables; rendered inline
              via `create_artifact(artifact_type="html", payload={"html_content": ...})`)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Opening token, JSON payload, closing token. The closing token uses the
# same kind string as the opening one. We capture the kind so we can match
# the right closing tag.
MARKER_PATTERN = re.compile(
    r"◤([A-Z_]+)◤(\{[^◤]*?\})◤END_\1◤",
    re.DOTALL,
)

SUPPORTED_KINDS = frozenset({"MD_DOCX", "HTML_DOCX", "PPTX", "DASHBOARD"})


@dataclass
class Marker:
    """A parsed marker occurrence."""

    kind: str                     # e.g. "MD_DOCX"
    payload: dict[str, Any]       # parsed JSON body
    start: int                    # char offset of ◤ opening
    end: int                      # char offset one past ◤END_...◤ closing
    raw: str                      # the full raw marker text (for stripping)

    @property
    def filename(self) -> str:
        return str(self.payload.get("filename", "") or "")


def find_markers(text: str) -> list[Marker]:
    """Return all supported markers in ``text``, in order of appearance.

    Malformed JSON, unknown kinds, and mismatched open/close tags are
    silently skipped (we never fail the host message on a bad marker).
    """
    if not text:
        return []
    out: list[Marker] = []
    for m in MARKER_PATTERN.finditer(text):
        kind = m.group(1)
        if kind not in SUPPORTED_KINDS:
            logger.debug("Skipping unsupported marker kind %r", kind)
            continue
        try:
            payload = json.loads(m.group(2))
        except json.JSONDecodeError:
            logger.debug("Skipping marker %r with malformed JSON", kind)
            continue
        if not isinstance(payload, dict):
            continue
        if not payload.get("filename"):
            logger.warning(
                "Marker %r has empty/missing 'filename' key; payload keys=%r",
                kind,
                list(payload.keys()),
            )
        out.append(
            Marker(
                kind=kind,
                payload=payload,
                start=m.start(),
                end=m.end(),
                raw=m.group(0),
            )
        )
    return out


def strip_markers(text: str) -> str:
    """Remove all marker blocks from ``text`` and tidy up surrounding whitespace."""
    if not text:
        return text
    cleaned = MARKER_PATTERN.sub("", text)
    # Collapse 3+ newlines left behind by removals
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def iter_kind(markers: list[Marker], kind: str) -> Iterator[Marker]:
    """Yield markers of a specific kind (convenience)."""
    for m in markers:
        if m.kind == kind:
            yield m
