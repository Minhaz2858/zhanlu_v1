"""fuzzy_match tool — fuzzy text find/replace for agent file edits.

When an LLM proposes an edit like ``edit_file(old="...", new="...")`` the
exact text may not match (different whitespace, escaped newlines, etc).
This tool implements the 8-strategy matching chain from hermes' fuzzy_match:

  1. Exact match
  2. Line-trimmed
  3. Whitespace normalized
  4. Indentation flexible
  5. Escape normalized (\\n -> newline)
  6. Trimmed boundary
  7. Block anchor (first+last line + similarity)
  8. Context-aware (50% line similarity)

Returns the matched strategy name and the new content after replacement.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


# Common escape sequences the LLM tends to write into code
_UNICODE_MAP = {
    "\u201c": '"', "\u201d": '"',
    "\u2018": "'", "\u2019": "'",
    "\u2014": "--", "\u2013": "-",
    "\u2026": "...", "\u00a0": " ",
}


def _normalize_unicode(text: str) -> str:
    for k, v in _UNICODE_MAP.items():
        text = text.replace(k, v)
    return text


def _line_trim(line: str) -> str:
    return line.strip()


def _ws_normalize(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def _indent_flexible(text: str) -> str:
    return re.sub(r"^[ \t]+", "", text, flags=re.MULTILINE)


def _escape_normalize(text: str) -> str:
    return text.replace("\\n", "\n").replace("\\t", "\t").replace("\\\"", '"')


def _find_line_range(content: str, target: str, strategy: str) -> Optional[Tuple[int, int]]:
    """Return (start_line, end_line) where target is found, or None."""
    lines = content.split("\n")
    target_lines = target.split("\n")
    if not target_lines:
        return None

    def normalize(line: str, strat: str) -> str:
        if strat == "line_trim":
            return _line_trim(line)
        if strat == "ws_normalize":
            return _ws_normalize(line)
        if strat == "indent_flexible":
            return _indent_flexible(line)
        if strat == "escape_normalize":
            return _escape_normalize(line)
        return line

    for i in range(len(lines) - len(target_lines) + 1):
        chunk = lines[i:i + len(target_lines)]
        if all(
            normalize(c, strategy) == normalize(t, strategy)
            for c, t in zip(chunk, target_lines)
        ):
            return (i, i + len(target_lines))
    return None


def _block_anchor_match(content: str, target: str, threshold: float = 0.5) -> Optional[Tuple[int, int]]:
    """Block-anchor: first+last line must match, middle lines are matched by similarity."""
    content_lines = content.split("\n")
    target_lines = target.split("\n")
    if len(target_lines) < 3:
        return None
    first, last = target_lines[0].strip(), target_lines[-1].strip()
    for i in range(len(content_lines) - len(target_lines) + 1):
        if content_lines[i].strip() != first:
            continue
        end_idx = i + len(target_lines) - 1
        if end_idx >= len(content_lines) or content_lines[end_idx].strip() != last:
            continue
        middle_target = target_lines[1:-1]
        middle_content = content_lines[i + 1:end_idx]
        # If counts differ, allow some leeway via similarity
        if len(middle_target) != len(middle_content):
            return None
        matches = sum(
            1 for a, b in zip(middle_content, middle_target)
            if difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio() >= threshold
        )
        if matches == len(middle_target):
            return (i, end_idx + 1)
    return None


def fuzzy_find_and_replace(
    content: str, old: str, new: str, replace_all: bool = False
) -> Tuple[Optional[str], int, str, Optional[str]]:
    """Find ``old`` in ``content`` and replace with ``new`` using a chain of strategies.

    Returns ``(new_content, match_count, strategy, error)``. On failure,
    ``new_content`` is None and ``error`` explains why.
    """
    if not old:
        return None, 0, "", "old text is empty"
    strategies = [
        "exact", "line_trim", "ws_normalize",
        "indent_flexible", "escape_normalize",
        "trimmed_boundary", "block_anchor", "context_aware",
    ]
    # Normalize escapes in both content and target
    content_norm = _normalize_unicode(content)
    old_norm = _normalize_unicode(old)
    new_norm = _normalize_unicode(new)

    # Strategy 1: exact match
    count = content_norm.count(old_norm)
    if count > 0:
        if replace_all:
            return content_norm.replace(old_norm, new_norm, -1), count, "exact", None
        if count > 1:
            return None, count, "exact", f"Found {count} exact matches; pass replace_all=True to replace all."
        return content_norm.replace(old_norm, new_norm, 1), 1, "exact", None

    # Strategies 2–5: line-based normalization
    for strat in ("line_trim", "ws_normalize", "indent_flexible", "escape_normalize"):
        rng = _find_line_range(content_norm, old_norm, strat)
        if rng is None:
            continue
        start, end = rng
        lines = content_norm.split("\n")
        new_lines = new_norm.split("\n")
        result_lines = lines[:start] + new_lines + lines[end:]
        return "\n".join(result_lines), 1, strat, None

    # Strategy 6: trimmed boundary
    old_trimmed = old_norm.strip("\n")
    if old_trimmed != old_norm:
        idx = content_norm.find(old_trimmed)
        if idx >= 0:
            new_content = (
                content_norm[:idx]
                + new_norm.strip("\n")
                + content_norm[idx + len(old_trimmed):]
            )
            return new_content, 1, "trimmed_boundary", None

    # Strategy 7: block anchor
    rng = _block_anchor_match(content_norm, old_norm)
    if rng is not None:
        start, end = rng
        lines = content_norm.split("\n")
        new_lines = new_norm.split("\n")
        result_lines = lines[:start] + new_lines + lines[end:]
        return "\n".join(result_lines), 1, "block_anchor", None

    # Strategy 8: context-aware (50% similarity per line)
    rng = _block_anchor_match(content_norm, old_norm, threshold=0.5)
    if rng is not None:
        start, end = rng
        lines = content_norm.split("\n")
        new_lines = new_norm.split("\n")
        result_lines = lines[:start] + new_lines + lines[end:]
        return "\n".join(result_lines), 1, "context_aware", None

    return None, 0, "", f"Could not match the old text in the file using any of the {len(strategies)} strategies."


async def _fuzzy_match(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    content = args.get("content", "")
    old = args.get("old", "")
    new = args.get("new", "")
    replace_all = bool(args.get("replace_all", False))
    if not content:
        return {"success": False, "error": "content is required"}
    if not old:
        return {"success": False, "error": "old is required"}
    new_content, count, strategy, error = fuzzy_find_and_replace(content, old, new, replace_all)
    if error:
        return {"success": False, "error": error, "match_count": count, "strategy": strategy}
    return {
        "success": True,
        "match_count": count,
        "strategy": strategy,
        "new_content": new_content,
    }


FUZZY_MATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fuzzy_match",
        "description": (
            "Fuzzy find-and-replace for text content. Tries 8 matching "
            "strategies (exact, line-trimmed, whitespace-normalized, "
            "indentation-flexible, escape-normalized, trimmed-boundary, "
            "block-anchor, context-aware). Use when an exact string match "
            "fails but the text is conceptually the same. Returns the "
            "matched strategy and the new content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The full text to search in."},
                "old": {"type": "string", "description": "The substring to find."},
                "new": {"type": "string", "description": "The replacement."},
                "replace_all": {"type": "boolean", "description": "If true, replace all matches; otherwise fail when more than one match is found.", "default": False},
            },
            "required": ["content", "old", "new"],
        },
    },
}


registry.register(
    name="fuzzy_match",
    schema=FUZZY_MATCH_SCHEMA,
    handler=_fuzzy_match,
    category="files",
    toolset="files",
    description="Fuzzy find-and-replace for text content.",
    emoji="🔍",
    max_result_size_chars=50_000,
)
