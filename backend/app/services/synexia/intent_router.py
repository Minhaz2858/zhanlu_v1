"""File-format intent detection.

A small, dependency-free helper that scans a user message for explicit
file-format keywords (``docx`` / ``pptx`` / ``xlsx`` / ``pdf`` / ``md``)
and returns the matched format, or ``None`` if no file-format intent is
present.

This is the single source of truth for "did the user ask for a file in
format X?" — used by:

* ``synexia/finalize.py`` — to set the right ``user_signal`` and
  pre-trigger ``run_sandbox_skill`` so the first response already
  contains a downloadable artifact.
* ``agent_prompts.py`` — to inject a routing rule into the system
  prompt for the General Assistant.
* Frontend callers that want to mirror the backend's intent decision.

The check is intentionally cheap (no LLM call) so it sits in the hot
path of every chat turn.  If we ever want fancier intent detection
(LLM classifier, embedding-similarity to a labeled set), it slots in
here — the public function ``detect_file_intent`` stays the same.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

# Recognized file formats.  Order matters: the regex tries them in this
# order, so we put the more specific multi-letter tokens first ("docx"
# before "doc", "pptx" before "ppt").  This avoids "docx" matching as
# "doc" first and returning the wrong format.
FileFormat = Literal["docx", "pptx", "xlsx", "pdf", "md", "html", "dashboard"]

# Prefix patterns.  We use a word-boundary + case-insensitive match on
# the *first 3+ letters* of each keyword so the helper still picks up
# natural-language variants like "docx file", "PDF report",
# "PowerPoint (.pptx)", "Excel workbook (xlsx)", "markdown summary".
#
# - "pptx" prefix also matches "ppt" / "pptx" / "powerpoint"
# - "xlsx" prefix also matches "xls" / "xlsx" / "excel"
# - "md"   prefix also matches "md" / "markdown"
_FORMAT_PATTERNS: tuple[tuple[FileFormat, str], ...] = (
    ("docx", r"\b(?:docx|word\s*document)\b|\.docx?\b|公文|报告文档"),
    ("pptx", r"\b(?:pptx|powerpoint|\.ppt|presentation\s*deck|slide\s*deck|pitch\s*deck|project\s*deck)\b"),
    ("xlsx", r"\b(?:xlsx|excel\s*workbook|spreadsheet)\b|\.xls\b"),
    ("pdf",  r"\b(?:pdf)\b"),
    ("md",   r"\bmarkdown\b|\.md\b"),
    ("html", r"\b(?:html|\.html?|web\s*page|webpage)\b"),
    ("dashboard", r"\b(?:dashboard|dash\-board|kpi\s*dashboard)\b"),
)

# Compile once.  We re-use these patterns in ``detect_file_intent``.
_COMPILED: dict[FileFormat, re.Pattern[str]] = {
    fmt: re.compile(pat, re.IGNORECASE) for fmt, pat in _FORMAT_PATTERNS
}


def detect_file_intent(text: Optional[str]) -> Optional[FileFormat]:
    """Return the file format the user asked for, or ``None``.

    Examples
    --------
    >>> detect_file_intent("make me a DOCX sales report")
    'docx'
    >>> detect_file_intent("can I have it as a PowerPoint?")
    'pptx'
    >>> detect_file_intent("export to xlsx please")
    'xlsx'
    >>> detect_file_intent("send me the markdown summary")
    'md'
    >>> detect_file_intent("hello, how are you?")
    None
    >>> detect_file_intent("")
    None
    >>> detect_file_intent(None)
    None
    """
    if not text:
        return None
    # Iterate in priority order — first hit wins.
    for fmt, pattern in _COMPILED.items():
        if pattern.search(text):
            return fmt
    return None


# Map format → "export_<fmt>" user_signal string used by
# ``synexia/user_signal.py`` and ``MessageBubble.jsx``.
EXPORT_SIGNAL_BY_FORMAT: dict[FileFormat, str] = {
    "docx": "export_docx",
    "pptx": "export_pptx",
    "xlsx": "export_xlsx",
    "pdf":  "export_pdf",
    "md":   "export_md",
    "html": "export_html",
    "dashboard": "export_dashboard",
}


def user_signal_for_format(fmt: FileFormat) -> str:
    """Return the canonical user_signal string for a given file format."""
    return EXPORT_SIGNAL_BY_FORMAT[fmt]
