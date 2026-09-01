"""Session Memory compaction — deterministic one-line summaries.

This is the second cheapest compaction layer: no LLM call required.
Old messages are summarized into one-line descriptions ("role: truncated text")
and combined into a single "session memory summary" message.
"""

from __future__ import annotations

import logging
from typing import Any

from .token_estimator import estimate_messages_tokens
from .attachments import build_compact_attachments, render_compact_attachment

log = logging.getLogger(__name__)

SESSION_MEMORY_KEEP_RECENT = 12
SESSION_MEMORY_MAX_LINES = 48
SESSION_MEMORY_MAX_CHARS = 4_000


def _summarize_message_for_memory(message: dict[str, Any]) -> str:
    """Produce a one-line summary of a message."""
    role = message.get("role", "unknown")
    content = message.get("content", "")

    if isinstance(content, str) and content.strip():
        text = " ".join(content.split())
        if text:
            text = text[:160]
            return f"{role}: {text}"

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        names = []
        for tc in tool_calls:
            if isinstance(tc, dict):
                func = tc.get("function", {})
                if isinstance(func, dict):
                    name = func.get("name", "")
                    if name:
                        names.append(name)
        if names:
            return f"{role}: tool calls -> {', '.join(names[:4])}"

    if role == "tool":
        return f"{role}: tool results returned"

    return f"{role}: [non-text content]"


def _build_session_memory_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Build a single summary message from a list of older messages."""
    lines: list[str] = []
    total_chars = 0

    for message in messages:
        line = _summarize_message_for_memory(message)
        if not line:
            continue
        projected = total_chars + len(line) + 1
        if lines and (len(lines) >= SESSION_MEMORY_MAX_LINES or projected >= SESSION_MEMORY_MAX_CHARS):
            lines.append("... earlier context condensed ...")
            break
        lines.append(line)
        total_chars = projected

    if not lines:
        return None

    body = "\n".join(lines)
    return {
        "role": "user",
        "content": "Session memory summary from earlier in this conversation:\n" + body,
    }


def _split_preserving_tool_pairs(
    messages: list[dict[str, Any]],
    *,
    preserve_recent: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split older/newer segments without cutting through a tool_use/result pair."""
    if len(messages) <= preserve_recent:
        return [], list(messages)

    split_index = max(0, len(messages) - preserve_recent)

    while split_index > 0:
        prev_msg = messages[split_index - 1]
        curr_msg = messages[split_index]
        if (
            prev_msg.get("role") == "assistant"
            and isinstance(prev_msg.get("tool_calls"), list)
            and prev_msg["tool_calls"]
            and curr_msg.get("role") == "tool"
        ):
            split_index -= 1
            continue
        break

    older = list(messages[:split_index])
    newer = list(messages[split_index:])
    return older, newer


def try_session_memory_compaction(
    messages: list[dict[str, Any]],
    *,
    preserve_recent: int = SESSION_MEMORY_KEEP_RECENT,
    trigger: str = "auto",
    metadata: dict[str, Any] | None = None,
) -> "CompactionResult | None":
    """Cheap deterministic compaction for long chats before full LLM compaction.

    Returns None if compaction isn't applicable or wouldn't save tokens.
    """
    # Import here to avoid circular import
    from . import CompactionResult, create_compact_boundary_message

    if len(messages) <= preserve_recent + 4:
        return None

    older, newer = _split_preserving_tool_pairs(messages, preserve_recent=preserve_recent)
    if not older:
        return None

    summary_message = _build_session_memory_message(older)
    if summary_message is None:
        return None

    provisional = [summary_message, *newer]

    if (
        estimate_messages_tokens(provisional) >= estimate_messages_tokens(messages)
        and len(provisional) >= len(messages)
    ):
        return None

    compact_metadata = {
        "trigger": trigger,
        "compact_kind": "session_memory",
        "pre_compact_message_count": len(messages),
        "pre_compact_token_count": estimate_messages_tokens(messages),
        "preserve_recent": preserve_recent,
        "used_session_memory": True,
    }

    result = CompactionResult(
        trigger=trigger,
        compact_kind="session_memory",
        boundary_message=create_compact_boundary_message(compact_metadata),
        summary_messages=[summary_message],
        messages_to_keep=list(newer),
        attachments=build_compact_attachments(older, metadata=metadata),
        compact_metadata=compact_metadata,
    )

    # Finalize: compute post-compact stats
    post_messages = [result.boundary_message, *result.summary_messages, *result.messages_to_keep]
    for att in result.attachments:
        post_messages.append(render_compact_attachment(att))

    result.compact_metadata["post_compact_message_count"] = len(post_messages)
    result.compact_metadata["post_compact_token_count"] = estimate_messages_tokens(post_messages)
    result.boundary_message = create_compact_boundary_message(result.compact_metadata)

    return result
