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


# ---------------------------------------------------------------------------
# Deck-edit intent detection
# ---------------------------------------------------------------------------
#
# Mirrors ``detect_file_intent``: cheap, dependency-free, regex-only.  Maps a
# user message to one of the six deck-edit tools, or ``None`` when the message
# is not an edit request.  Regeneration phrasing ("redo", "from scratch",
# "regenerate") short-circuits to ``None`` so the caller keeps the full
# regeneration path instead of editing an existing artifact.

# Recognized deck-edit tools.  Order matters: more specific patterns win.
DeckEditTool = Literal[
    "edit_slide", "add_slide", "restyle_deck", "update_chart",
    "remove_slide", "reorder_slide",
]

# Regeneration phrases — these mean "throw the old one away and rebuild", NOT
# "edit the existing deck".  Checked before the edit patterns.
_DECK_EDIT_REGENERATE_RE = re.compile(
    r"\b(?:regenerate|re-?generate|redo|re-?do|from\s+scratch|re-?make|remake)\b"
    r"|重新生成|重做|重来|从头生成|重新做",
    re.IGNORECASE,
)

# Slide index fragment: digits or common Chinese numerals (一二三四五六七八九十).
# e.g. "第3页", "第三页", "第一页".
_SLIDE_IDX = r"[0-9一二三四五六七八九十百]*"

_DECK_EDIT_INTENT_PATTERNS: tuple[tuple[DeckEditTool, str], ...] = (
    (
        "remove_slide",
        r"\b(?:remove|delete)\s+(?:the\s+)?(?:slide|page)\b"
        r"|删除第?\s*" + _SLIDE_IDX + r"\s*[页張张]"
        r"|删掉第?\s*" + _SLIDE_IDX + r"\s*[页張张]"
        r"|移除第?\s*" + _SLIDE_IDX + r"\s*[页張张]",
    ),
    (
        "reorder_slide",
        r"\b(?:reorder|re-?order|rearrange)\b|调整顺序|调换顺序"
        r"|移动第?\s*" + _SLIDE_IDX + r"\s*[页張张]",
    ),
    (
        "add_slide",
        r"\b(?:add|insert)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:slide|page)\b"
        r"|添加一页|新增一页|加一页|加个页面|增加一页",
    ),
    (
        "update_chart",
        r"\b(?:update|change|modify|edit)\s+(?:the\s+)?chart\b"
        r"|修改图表|更新图表|改图表|改下图表|调整图表",
    ),
    (
        "restyle_deck",
        r"\b(?:change|switch|swap)\s+(?:the\s+)?(?:to\s+)?(?:a\s+|an\s+)?"
        r"(?:different\s+)?(?:theme|style|color\s*scheme)\b"
        r"|换个主题|换主题|改主题|更换主题|换个风格|改风格|换风格",
    ),
    (
        "edit_slide",
        r"\b(?:edit|update|change|modify|revise|rewrite)\s+(?:the\s+)?(?:slide|page)\b"
        r"|修改第?\s*" + _SLIDE_IDX + r"\s*[页張张]"
        r"|编辑第?\s*" + _SLIDE_IDX + r"\s*[页張张]"
        r"|改第?\s*" + _SLIDE_IDX + r"\s*[页張张]",
    ),
)

_DECK_EDIT_COMPILED: dict[DeckEditTool, re.Pattern[str]] = {
    tool: re.compile(pat, re.IGNORECASE)
    for tool, pat in _DECK_EDIT_INTENT_PATTERNS
}


def detect_deck_edit_intent(text: Optional[str]) -> Optional[str]:
    """Return the deck-edit tool name for a message, or ``None``.

    Regeneration phrasing (``regenerate`` / ``redo`` / ``from scratch`` /
    ``重新生成`` / ``重做`` ...) short-circuits to ``None`` so the caller
    keeps the full regeneration path.
    """
    if not text:
        return None
    if _DECK_EDIT_REGENERATE_RE.search(text):
        return None
    for tool, pattern in _DECK_EDIT_COMPILED.items():
        if pattern.search(text):
            return tool
    return None
