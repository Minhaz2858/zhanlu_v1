"""RAG citation grounding — parse [source: ...] markers in LLM responses.

When the RAG research agent returns tool results, each result includes metadata
(document name, URL, page number). This module:

1. **Annotates** tool results with ``[source: {name}]`` markers appended to
   each chunk so the LLM can cite them naturally.
2. **Parses** LLM responses for ``[source: ...]`` / ``[citation: ...]`` markers
   and normalizes them into structured ``Citation`` objects.
3. **Resolves** citations to their document metadata for frontend rendering
   (clickable links, tooltips with page numbers).

Design: citations are markdown-safe — the LLM writes ``[source: doc-name]``
inline in its response text; the parser extracts them without modifying the
text content (the markers remain visible as-is for plain-text clients,
while the frontend renders them as clickable badges).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Regex for [source: ...] and [citation: ...] markers
_MARKER_RE = re.compile(r"\[(source|citation):\s*([^\]]+)\]", re.IGNORECASE)

# Max metadata bytes per tool result (to avoid bloating tool result strings)
_MAX_METADATA_BYTES = 4096


@dataclass
class Citation:
    """A single resolved citation from an LLM response."""

    label: str           # e.g. "source: Quarterly Report 2025"
    source_name: str     # e.g. "Quarterly Report 2025"
    source_type: str = "document"  # "document" | "url" | "page"
    url: str = ""
    page: Optional[int] = None
    chunk_index: int = 0
    position_start: int = 0   # byte offset in the original text
    position_end: int = 0     # byte offset in the original text

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "source_name": self.source_name,
            "source_type": self.source_type,
            "url": self.url,
            "page": self.page,
            "chunk_index": self.chunk_index,
            "position_start": self.position_start,
            "position_end": self.position_end,
        }


def annotate_tool_result(
    content: str,
    source_name: str,
    source_type: str = "document",
    url: str = "",
    page: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> str:
    """Annotate a tool result chunk with citation metadata.

    Appends a ``[source: {name}]`` marker to the content so the LLM
    can reference it. Also includes URL and page in a compact footer
    if the metadata exceeds the chunk size.

    Args:
        content: The tool result text chunk.
        source_name: Display name for the source (e.g. "Q3 Report.pdf").
        source_type: "document", "url", or "page".
        url: Optional URL for the source.
        page: Optional page number.
        metadata: Optional extra metadata dict.

    Returns:
        The annotated content string.
    """
    marker = f"\n[source: {source_name}]"
    footer_parts = []
    if url:
        footer_parts.append(f"url: {url}")
    if page is not None:
        footer_parts.append(f"page: {page}")
    if metadata:
        for k, v in metadata.items():
            if k not in ("url", "page", "source_name"):
                footer_parts.append(f"{k}: {v}")

    if footer_parts:
        footer = f"\n<!-- citation_meta: {'; '.join(footer_parts[:10])} -->"
        return content + marker + footer
    return content + marker


def parse_citations(text: str) -> list[Citation]:
    """Extract all citation markers from LLM response text.

    Returns a list of ``Citation`` objects with positions and labels.
    The text itself is NOT modified — markers remain inline.

    Args:
        text: The full LLM response text.

    Returns:
        List of citations found, in order of appearance.
    """
    citations: list[Citation] = []
    for match in _MARKER_RE.finditer(text):
        source_name = match.group(2).strip()
        marker_type = match.group(1).lower()
        citations.append(Citation(
            label=f"{marker_type}: {source_name}",
            source_name=source_name,
            source_type="document",
            position_start=match.start(),
            position_end=match.end(),
        ))
    return citations


def resolve_citations(
    citations: list[Citation],
    source_registry: dict[str, dict],
) -> list[Citation]:
    """Resolve parsed citations against a source registry.

    The registry maps source names to metadata dicts:
        {"Q3 Report.pdf": {"url": "...", "page": 5, "type": "document"}}

    Args:
        citations: List of parsed citations from ``parse_citations()``.
        source_registry: Dict mapping source names to metadata.

    Returns:
        The same list with resolved metadata (url, page, source_type).
    """
    for c in citations:
        meta = source_registry.get(c.source_name)
        if not meta:
            # Try case-insensitive match
            name_lower = c.source_name.lower()
            for key, val in source_registry.items():
                if key.lower() == name_lower:
                    meta = val
                    break
        if meta:
            c.url = meta.get("url", "")
            c.page = meta.get("page")
            c.source_type = meta.get("type", "document")
    return citations


def citation_instruction() -> str:
    """Return a system prompt fragment instructing the LLM how to cite sources.

    Append this to the system prompt when RAG tool results contain citation
    metadata (annotated via ``annotate_tool_result``).
    """
    return (
        "\n\nWhen using information from tools, cite your sources inline "
        "using [source: name] markers (e.g., [source: Q3 Report.pdf]). "
        "Prefer citing the most specific source available. "
        "Do NOT fabricate sources — only cite documents actually provided by tools."
    )


__all__ = [
    "Citation",
    "annotate_tool_result",
    "parse_citations",
    "resolve_citations",
    "citation_instruction",
]
