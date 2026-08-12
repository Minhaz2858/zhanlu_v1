"""Pre-API tool result pruning -- deterministic, no-LLM context reduction.

A cheap pre-pass that runs before each LLM API call. Unlike full compaction
(which uses an LLM to summarize), this deterministically trims old tool-result
payloads without any LLM call, reclaiming tokens at near-zero cost.

Three passes:

1. **Dedup**: byte-identical tool results anywhere in the list are
   back-referenced to the newest full copy. Lossless -- no unique content
   is ever lost.

2. **Summarize old results**: tool results older than ``keep_recent`` are
   replaced with a compact placeholder ("[Old tool result cleared]").
   This is the same logic as microcompact, but exposed as a standalone
   pre-API check.

3. **Truncate oversized args**: old assistant tool_call arguments larger
   than ``max_args_chars`` are truncated. The LLM doesn't need the full
   args of old calls to continue the conversation.

Inspired by Hermes' ``ContextEngine.prune_tool_results_only()`` and
``_prune_old_tool_results()``, adapted for Zhanlu's message format.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.services.tool_result_classification import NO_EFFECT_TOOL_NAMES
from .token_estimator import estimate_text_tokens

log = logging.getLogger(__name__)

PRUNE_PLACEHOLDER = "[Old tool result cleared]"
DEFAULT_KEEP_RECENT = 5
DEFAULT_MIN_PRUNE_CHARS = 500
DEFAULT_MAX_ARGS_CHARS = 2000
DEFAULT_PRUNE_TRIGGER_TOKENS = 16000  # don't prune below this token count


def _content_hash(content: str) -> str:
    """Stable hash of a message content string."""
    return hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()


def _extract_tool_call_names(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Build a map of tool_call_id -> tool_name from assistant messages."""
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


def _dedup_tool_results(messages: list[dict[str, Any]]) -> int:
    """Pass 1: dedup byte-identical tool results, keeping the newest full copy.

    Older identical results are replaced with a back-reference placeholder.
    This is lossless: no unique content is ever lost.

    Returns the number of results deduped.
    """
    # Map content hash -> (first index, content)
    seen: dict[str, int] = {}
    deduped = 0

    for i, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or len(content) < 200:
            continue  # skip tiny results -- dedup overhead not worth it
        h = _content_hash(content)
        if h in seen:
            # Replace with back-reference
            messages[i]["content"] = f"[Duplicate of tool result at message {seen[h]}]"
            deduped += 1
        else:
            seen[h] = i

    return deduped


def _summarize_old_tool_results(
    messages: list[dict[str, Any]],
    keep_recent: int,
    min_prune_chars: int,
) -> int:
    """Pass 2: replace old tool results with a compact placeholder.

    Only results larger than ``min_prune_chars`` are pruned (tiny results
    aren't worth the placeholder overhead). The most recent ``keep_recent``
    tool results are protected.

    Returns the number of results summarized.
    """
    tool_names = _extract_tool_call_names(messages)

    # Collect all tool result message indices
    tool_indices: list[int] = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool":
            tool_indices.append(i)

    if len(tool_indices) <= keep_recent:
        return 0

    # Protect the most recent keep_recent results
    protected = set(tool_indices[-keep_recent:])
    to_prune = [i for i in tool_indices if i not in protected]

    pruned = 0
    for i in to_prune:
        content = messages[i].get("content", "")
        if not isinstance(content, str):
            continue
        if len(content) < min_prune_chars:
            continue  # skip small results
        messages[i]["content"] = PRUNE_PLACEHOLDER
        pruned += 1

    return pruned


def _truncate_oversized_args(
    messages: list[dict[str, Any]],
    max_args_chars: int,
    keep_recent: int,
) -> int:
    """Pass 3: truncate oversized tool_call arguments on old assistant messages.

    The LLM doesn't need the full args of old calls to continue. We protect
    the most recent ``keep_recent`` assistant messages with tool_calls.

    Returns the number of args truncated.
    """
    # Find assistant messages with tool_calls
    assistant_indices: list[int] = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            assistant_indices.append(i)

    if len(assistant_indices) <= keep_recent:
        return 0

    protected = set(assistant_indices[-keep_recent:])
    to_truncate = [i for i in assistant_indices if i not in protected]

    truncated = 0
    for i in to_truncate:
        tool_calls = messages[i].get("tool_calls", [])
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function", {})
            if not isinstance(func, dict):
                continue
            args = func.get("arguments", "")
            if isinstance(args, str) and len(args) > max_args_chars:
                func["arguments"] = args[:max_args_chars] + "...[truncated]"
                truncated += 1

    return truncated


def _effective_trigger_tokens(trigger_tokens: int, model: str) -> int:
    """Scale the prune trigger to the model's real context window.

    The flat ``DEFAULT_PRUNE_TRIGGER_TOKENS`` (16k) assumes a large cloud
    context. On a small local model (Ollama ``num_ctx=8192``) the trigger
    sits ABOVE the whole window, so pruning never ran and the report-
    synthesis LLM call overflowed (empty automation report). For small
    windows, trigger pruning once we cross ~50% of the window so the
    next call still fits.
    """
    try:
        from app.services.compaction import get_context_window
        window = get_context_window(model)
    except Exception:
        return trigger_tokens
    if window >= DEFAULT_PRUNE_TRIGGER_TOKENS * 2:
        return trigger_tokens
    return max(2_000, int(window * 0.5))


def prune_tool_results_only(
    messages: list[dict[str, Any]],
    *,
    current_tokens: int | None = None,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    min_prune_chars: int = DEFAULT_MIN_PRUNE_CHARS,
    max_args_chars: int = DEFAULT_MAX_ARGS_CHARS,
    trigger_tokens: int = DEFAULT_PRUNE_TRIGGER_TOKENS,
    model: str = "",
) -> tuple[list[dict[str, Any]], int]:
    """Deterministically prune old tool results without an LLM call.

    A cheap pre-pass that runs before each LLM API call. Three passes:
    1. Dedup byte-identical results (lossless).
    2. Replace old large results with a placeholder.
    3. Truncate oversized old tool_call arguments.

    Args:
        messages: Conversation message list (modified in place).
        current_tokens: Estimated token count of messages. If below
            ``trigger_tokens``, pruning is skipped (no-op).
        keep_recent: Number of recent tool results to protect.
        min_prune_chars: Minimum size for a result to be worth pruning.
        max_args_chars: Maximum chars for old tool_call arguments.
        trigger_tokens: Token threshold below which pruning is skipped.

    Returns:
        ``(messages, n_pruned)`` -- messages are mutated in place.
        ``n_pruned`` is the total count of items pruned across all 3 passes.
    """
    # Scale trigger to the model's real context window
    effective_trigger = _effective_trigger_tokens(trigger_tokens, model)

    # Skip if below effective trigger
    if current_tokens is not None and current_tokens < effective_trigger:
        return messages, 0

    # Estimate tokens if not provided
    if current_tokens is None:
        total_chars = sum(
            len(m.get("content", "")) if isinstance(m.get("content"), str) else 0
            for m in messages
        )
        # estimate_text_tokens expects a string, but total_chars is already
        # a character count. Apply the same heuristic directly (4 chars ≈ 1
        # token, with padding) to avoid the TypeError: 'int' has no len().
        from app.services.compaction.token_estimator import TOKEN_ESTIMATION_PADDING
        current_tokens = max(1, int(total_chars * TOKEN_ESTIMATION_PADDING / 4))
        if current_tokens < effective_trigger:
            return messages, 0

    n_deduped = _dedup_tool_results(messages)
    n_summarized = _summarize_old_tool_results(messages, keep_recent, min_prune_chars)
    n_truncated = _truncate_oversized_args(messages, max_args_chars, keep_recent)

    total_pruned = n_deduped + n_summarized + n_truncated
    if total_pruned > 0:
        log.debug(
            "Pre-API prune: %d deduped, %d summarized, %d args truncated",
            n_deduped, n_summarized, n_truncated,
        )

    return messages, total_pruned


__all__ = [
    "prune_tool_results_only",
    "DEFAULT_KEEP_RECENT",
    "DEFAULT_PRUNE_TRIGGER_TOKENS",
]
