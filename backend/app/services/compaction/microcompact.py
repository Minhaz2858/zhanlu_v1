"""Microcompact — clear old tool result content to reduce tokens cheaply.

This is the cheapest compaction layer: no LLM call required. Old tool
results from compactable tools (read_file, bash, grep, web_search, etc.)
are replaced with a placeholder message.

Adapted from OpenHarness's microcompact_messages() for Zhanlu's dict-based
message format where tool results appear as {"role": "tool", "content": ...}
messages following an assistant message with tool_calls.
"""

from __future__ import annotations

import logging
from typing import Any

from .token_estimator import estimate_text_tokens

log = logging.getLogger(__name__)

# Tools whose results can be safely cleared (they're read-only / informational)
COMPACTABLE_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "write_file",
    "web_search",
    "web_extract",
    "execute_code",
    "list_tools",
    "list_market_agents",
    "list_knowledge_bases",
})

TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"
DEFAULT_KEEP_RECENT = 5


def _extract_tool_call_names(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Build a map of tool_call_id → tool_name from assistant messages."""
    tool_names: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id", "")
            func = tc.get("function", {})
            name = func.get("name", "") if isinstance(func, dict) else ""
            if tc_id and name:
                tool_names[tc_id] = name
    return tool_names


def _collect_compactable_tool_ids(messages: list[dict[str, Any]]) -> list[str]:
    """Walk messages and collect tool_call IDs whose results are compactable."""
    tool_names = _extract_tool_call_names(messages)

    # Find all tool result messages (role="tool") and their tool_call_ids
    tool_result_ids: list[str] = []
    for msg in messages:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id and tc_id not in tool_result_ids:
                tool_result_ids.append(tc_id)

    # Filter to only compactable tools
    return [
        tc_id
        for tc_id in tool_result_ids
        if tool_names.get(tc_id, "") in COMPACTABLE_TOOLS
    ]


def microcompact_messages(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
) -> tuple[list[dict[str, Any]], int]:
    """Clear old compactable tool results, keeping the most recent `keep_recent`.

    Args:
        messages: Conversation message list (modified in place).
        keep_recent: Number of recent tool results to preserve.

    Returns:
        (messages, tokens_saved) — messages are mutated in place.
    """
    keep_recent = max(1, keep_recent)  # never clear ALL results
    all_ids = _collect_compactable_tool_ids(messages)

    if len(all_ids) <= keep_recent:
        return messages, 0

    keep_set = set(all_ids[-keep_recent:])
    clear_set = set(all_ids) - keep_set

    tokens_saved = 0
    for i, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        tc_id = msg.get("tool_call_id", "")
        if tc_id in clear_set:
            content = msg.get("content", "")
            if content != TIME_BASED_MC_CLEARED_MESSAGE:
                tokens_saved += estimate_text_tokens(content)
                messages[i] = {
                    **msg,
                    "content": TIME_BASED_MC_CLEARED_MESSAGE,
                    "_microcompacted": True,
                }

    if tokens_saved > 0:
        log.info(
            "Microcompact cleared %d tool results, saved ~%d tokens",
            len(clear_set),
            tokens_saved,
        )

    return messages, tokens_saved
