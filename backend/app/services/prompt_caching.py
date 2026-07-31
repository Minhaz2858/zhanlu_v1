"""Prompt caching -- apply cache_control breakpoints for prefix caching.

Two strategies depending on provider:

1. **Automatic prefix caching** (DeepSeek, OpenAI): the provider caches the
   longest matching prefix automatically. No explicit markers needed — just
   ensure the system prompt + tool definitions are stable at the start.
   This module is a no-op for these providers.

2. **Explicit cache_control** (Anthropic, OpenRouter): the provider requires
   explicit ``cache_control`` breakpoints on message content. This module
   applies up to 4 breakpoints: system prompt + last 3 non-system messages.

A config flag controls whether explicit markers are applied. When disabled
(the default for DeepSeek), the module is a no-op and the provider's
automatic caching handles prefix reuse.

Inspired by Hermes' ``agent/prompt_caching.py``.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Maximum cache_control breakpoints most providers support.
_MAX_BREAKPOINTS = 4


def _build_marker(ttl: str = "5m") -> dict[str, str]:
    """Build a cache_control marker dict for the given TTL."""
    marker: dict[str, str] = {"type": "ephemeral"}
    if ttl == "1h":
        marker["ttl"] = "1h"
    return marker


def _can_carry_marker(msg: dict) -> bool:
    """True if a message can carry a cache_control marker.

    Empty-content messages (e.g. assistant turns that are pure tool_calls)
    can't carry markers on the OpenAI-compatible format — skip them so
    breakpoints land on messages that actually have content.
    """
    content = msg.get("content")
    if content is None or content == "":
        return False
    if isinstance(content, list):
        return bool(content) and isinstance(content[-1], dict)
    return isinstance(content, str)


def _apply_cache_marker(msg: dict, marker: dict[str, str]) -> None:
    """Add cache_control to a single message, handling string and list content."""
    content = msg.get("content")

    if content is None or content == "":
        msg["cache_control"] = marker
        return

    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": marker}
        ]
        return

    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = marker


def apply_cache_control(
    messages: list[dict[str, Any]],
    *,
    enabled: bool = False,
    cache_ttl: str = "5m",
) -> list[dict[str, Any]]:
    """Apply cache_control breakpoints to messages for prefix caching.

    When ``enabled`` is False (default for DeepSeek), returns messages
    unchanged — the provider's automatic prefix caching handles it.

    When ``enabled`` is True (for Anthropic/OpenRouter), applies up to 4
    cache_control breakpoints: system prompt + last 3 non-system messages
    that can carry markers.

    Args:
        messages: The conversation messages (not modified in place).
        enabled: Whether to apply explicit cache_control markers.
        cache_ttl: Cache TTL ("5m" or "1h"). Default "5m".

    Returns:
        A (possibly new) list of messages with cache_control applied.
    """
    if not enabled or not messages:
        return messages

    # Deep copy so we don't mutate the caller's messages
    result = copy.deepcopy(messages)
    marker = _build_marker(cache_ttl)
    breakpoints_used = 0

    # Breakpoint 1: system prompt (if present)
    if result[0].get("role") == "system":
        _apply_cache_marker(result[0], marker)
        breakpoints_used += 1

    # Remaining breakpoints: last N non-system messages that can carry markers
    remaining = _MAX_BREAKPOINTS - breakpoints_used
    non_sys_indices = [
        i for i in range(len(result))
        if result[i].get("role") != "system" and _can_carry_marker(result[i])
    ]
    for idx in non_sys_indices[-remaining:]:
        _apply_cache_marker(result[idx], marker)

    logger.debug(
        "Applied %d cache_control breakpoints (ttl=%s)",
        min(_MAX_BREAKPOINTS, breakpoints_used + len(non_sys_indices[-remaining:])),
        cache_ttl,
    )
    return result


__all__ = ["apply_cache_control"]
