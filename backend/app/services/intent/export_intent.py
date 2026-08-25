"""Code-level intent router for export requests."""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Literal, Optional


@dataclass
class ExportIntent:
    format: Optional[str]
    source_execution_id: Optional[str]
    confidence: Literal["strong", "weak"]
    matched_pattern: str


_FORMAT_ALIASES = {
    "docx": "docx", "word": "docx", "ms word": "docx",
    "pdf": "pdf",
    "pptx": "pptx", "powerpoint": "pptx", "slides": "pptx",
    "xlsx": "xlsx", "excel": "xlsx", "spreadsheet": "xlsx",
    "html": "html", "web page": "html",
}

_STRONG_PATTERNS = [
    # verb ... in/as/to a? format   ("give me in docx", "export as PDF")
    (r"\b(?:give|send|export|save|download|make|convert|render)\b"
     r".*?\b(?:in|as|to)\b\s*(?:a\s+)?"
     r"(docx|pdf|pptx|xlsx|html|word|powerpoint|excel)\b"),
    # in/as a? format at end-of-line  ("in Word", "in pdf", "as xlsx",
    #                                  "now as PowerPoint")
    (r"\b(?:now|also)?\s*(?:in|as)\s+(?:a\s+)?"
     r"(docx|pdf|pptx|xlsx|html|word|powerpoint|excel)\s*$"),
    # make/create/generate a? format  ("make a Word file")
    (r"\b(?:make|create|generate)\s+(?:a|an|the)?\s*(?:ms\s+)?"
     r"(docx|pdf|pptx|xlsx|html|word|powerpoint|excel)\b"),
    # bare format                     ("docx")
    (r"^(docx|pdf|pptx|xlsx|html|word|powerpoint|excel)\s*$"),
]

_WEAK_PATTERNS = [
    r"\b(?:export|download|save\s+as|send\s+(?:it|the\s+same|that))\b",
    r"\b(?:re-?format|re-?export)\b",
    r"\b(?:now|also)\s+(?:as|in)\s+(?:a\s+)?(?:word|pdf|powerpoint|excel|docx|pptx)\b",
]


def detect_export_intent(user_message: str) -> Optional[ExportIntent]:
    if not user_message:
        return None
    msg = user_message.lower().strip()

    for pattern in _STRONG_PATTERNS:
        m = re.search(pattern, msg)
        if m:
            fmt_token = next((g for g in m.groups() if g), None)
            return ExportIntent(
                format=_FORMAT_ALIASES.get(fmt_token, fmt_token),
                source_execution_id=None,
                confidence="strong",
                matched_pattern=pattern,
            )

    for pattern in _WEAK_PATTERNS:
        if re.search(pattern, msg):
            return ExportIntent(
                format=None, source_execution_id=None,
                confidence="weak", matched_pattern=pattern,
            )

    return None
