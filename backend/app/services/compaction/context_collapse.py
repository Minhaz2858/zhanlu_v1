"""Context Collapse — deterministically shrink oversized text blocks.

This is a bridge between microcompact and full compact: it truncates
large text blocks (tool results, message content) to head+tail previews
without any LLM call.

Also includes PTL (Prompt Too Long) retry logic: when the compact
request itself is too large, drop the oldest prompt rounds.
"""

from __future__ import annotations

import logging
from typing import Any

from .token_estimator import estimate_messages_tokens

log = logging.getLogger(__name__)

CONTEXT_COLLAPSE_TEXT_CHAR_LIMIT = 2_400
CONTEXT_COLLAPSE_HEAD_CHARS = 900
CONTEXT_COLLAPSE_TAIL_CHARS = 500
PTL_RETRY_MARKER = "[earlier conversation truncated for compaction retry]"


def collapse_text(text: str) -> str:
    """Truncate a long text to head+tail with an omission marker."""
    if len(text) <= CONTEXT_COLLAPSE_TEXT_CHAR_LIMIT:
        return text
    omitted = len(text) - CONTEXT_COLLAPSE_HEAD_CHARS - CONTEXT_COLLAPSE_TAIL_CHARS
    head = text[:CONTEXT_COLLAPSE_HEAD_CHARS].rstrip()
    tail = text[-CONTEXT_COLLAPSE_TAIL_CHARS:].lstrip()
    return f"{head}\n...[collapsed {omitted} chars]...\n{tail}"


def try_context_collapse(
    messages: list[dict[str, Any]],
    *,
    preserve_recent: int,
) -> list[dict[str, Any]] | None:
    """Deterministically shrink oversized text blocks before full compact.

    Returns None if no changes were made (nothing to collapse).
    """
    if len(messages) <= preserve_recent + 2:
        return None

    split_idx = max(0, len(messages) - preserve_recent)
    older = messages[:split_idx]
    newer = messages[split_idx:]

    changed = False
    collapsed_older: list[dict[str, Any]] = []

    for msg in older:
        content = msg.get("content")
        if isinstance(content, str) and len(content) > CONTEXT_COLLAPSE_TEXT_CHAR_LIMIT:
            collapsed = collapse_text(content)
            if collapsed != content:
                changed = True
            collapsed_older.append({**msg, "content": collapsed})
        else:
            collapsed_older.append(msg)

    if not changed:
        return None

    result = [*collapsed_older, *newer]
    if estimate_messages_tokens(result) >= estimate_messages_tokens(messages):
        return None
    return result


def _group_messages_by_prompt_round(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Group messages into 'prompt rounds' separated by user messages.

    A new round starts when a user message (not a tool result) appears.
    """
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        starts_new_round = (
            message.get("role") == "user"
            and not message.get("tool_call_id")
            and bool(str(message.get("content", "")).strip())
        )
        if starts_new_round and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return groups


def truncate_head_for_ptl_retry(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Drop the oldest prompt rounds when the compact request itself is too large.

    Called when the LLM returns a 'prompt too long' error during compaction.
    """
    groups = _group_messages_by_prompt_round(messages)
    if len(groups) < 2:
        return None

    drop_count = max(1, len(groups) // 5)
    drop_count = min(drop_count, len(groups) - 1)
    retained = [msg for group in groups[drop_count:] for msg in group]
    if not retained:
        return None

    if retained[0].get("role") == "assistant":
        return [{"role": "user", "content": PTL_RETRY_MARKER}, *retained]
    return retained
