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

# Conservative default per-message tool-output cap (in tokens) when the
# model is not in TOOL_OUTPUT_CAP_BY_MODEL.  Set to deepseek's value so
# the worst-case overflow matches a 128k-context window's headroom.
DEFAULT_TOOL_OUTPUT_CAP_TOKENS = 24_576

# P1-5 fallback model for ``fallback_to_different_model`` tier.
# Larger-context alternative used when the user's selected model can't
# fit the conversation.  Currently the catalog default (deepseek-v4-flash,
# 128k context).
FALLBACK_MODEL_NAME = "deepseek-v4-flash"


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
    "smart_truncate",
    "escalate",
    "DEFAULT_KEEP_RECENT",
    "DEFAULT_PRUNE_TRIGGER_TOKENS",
    "DEFAULT_TOOL_OUTPUT_CAP_TOKENS",
    "FALLBACK_MODEL_NAME",
]


# ── P1-5: per-model tool-output cap + escalation ladder ───────────────────

def _resolve_tool_output_cap(model: str, tool_output_caps: dict | None = None) -> int:
    """Look up the per-model tool-output cap (in tokens) for ``model``.

    Resolution order:
      1. ``tool_output_caps`` arg (test override)
      2. ``settings.TOOL_OUTPUT_CAP_BY_MODEL`` JSON config
      3. :data:`DEFAULT_TOOL_OUTPUT_CAP_TOKENS` (24,576)
    """
    if tool_output_caps is None:
        try:
            import json as _json
            from app.config import settings as _settings
            _raw = getattr(_settings, "TOOL_OUTPUT_CAP_BY_MODEL", "") or ""
            if _raw:
                tool_output_caps = _json.loads(_raw)
        except Exception:
            tool_output_caps = None
    if not tool_output_caps:
        return DEFAULT_TOOL_OUTPUT_CAP_TOKENS
    if not model:
        return DEFAULT_TOOL_OUTPUT_CAP_TOKENS
    m = model.lower()
    # Longest-key-first so "deepseek-v4-flash" wins over "deepseek-chat"
    for key in sorted(tool_output_caps.keys(), key=len, reverse=True):
        if key and key.lower() in m:
            try:
                return int(tool_output_caps[key])
            except (TypeError, ValueError):
                continue
    return DEFAULT_TOOL_OUTPUT_CAP_TOKENS


def smart_truncate(
    messages: list[dict[str, Any]],
    model: str = "",
    tool_output_caps: dict | None = None,
    keep_recent: int = 2,
) -> list[dict[str, Any]]:
    """Cap oversized tool-role messages to the model's per-message limit.

    Walks the message list forward.  For each ``role == "tool"`` message
    whose ``content`` exceeds ``cap_tokens * 4`` characters, the content
    is truncated to that size with a trailing marker so the LLM can see
    it was elided.  The most recent ``keep_recent`` tool messages are
    never truncated (always trim older first).

    Returns a NEW list (does not mutate the input).
    """
    cap_tokens = _resolve_tool_output_cap(model, tool_output_caps)
    cap_chars = cap_tokens * 4

    # Find indices of all tool-role messages, ordered by position.
    tool_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool" and isinstance(m.get("content"), str)
    ]
    # The most recent `keep_recent` tool messages are protected.
    protected = set(tool_indices[-keep_recent:]) if keep_recent > 0 else set()

    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        if i in protected or m.get("role") != "tool" or not isinstance(m.get("content"), str):
            out.append(m)
            continue
        content = m["content"]
        if len(content) <= cap_chars:
            out.append(m)
            continue
        # Truncate with a marker so the LLM can tell the result was
        # trimmed, not just cut off mid-sentence.
        marker = f"\n[...truncated to {cap_tokens} tokens ({cap_chars} chars)...]"
        trimmed = content[: cap_chars - len(marker)] + marker
        out.append({**m, "content": trimmed})
    return out


def escalate(
    tier: int,
    messages: list[dict[str, Any]],
    model: str = "",
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Walk one tier of the CONTEXT_ESCALATION_LADDER.

    Tiers (from settings.CONTEXT_ESCALATION_LADDER):
      0 compact                    — mark a compaction run as needed
      1 truncate_tool_outputs      — apply smart_truncate
      2 drop_old_tool_messages     — drop the oldest half of tool msgs
      3 fallback_to_different_model — return messages + suggested model

    Returns:
        (messages, action_name, fallback_model_or_None)
        The caller decides whether to actually mutate state and whether
        to re-issue the LLM call.
    """
    # Resolve the ladder from config (default to the plan's 4 tiers).
    ladder = ["compact", "truncate_tool_outputs", "drop_old_tool_messages",
              "fallback_to_different_model"]
    try:
        import json as _json
        from app.config import settings as _settings
        _raw = getattr(_settings, "CONTEXT_ESCALATION_LADDER", "") or ""
        if _raw:
            parsed = _json.loads(_raw)
            if isinstance(parsed, list) and all(isinstance(s, str) for s in parsed):
                ladder = parsed
    except Exception:
        pass

    if tier < 0 or tier >= len(ladder):
        return messages, "noop", None

    action = ladder[tier]

    if action == "compact":
        # Caller runs the compactor; we just signal the action.
        return messages, action, None

    if action == "truncate_tool_outputs":
        new_msgs = smart_truncate(messages, model=model)
        return new_msgs, action, None

    if action == "drop_old_tool_messages":
        # Drop the oldest half of tool-role messages (keep the rest).
        # 2026-08-25: BUGFIX — when dropping a tool message, we must also
        # remove the matching tool_call from the prior assistant message,
        # otherwise DeepSeek/OpenAI reject the request with:
        #   "An assistant message with 'tool_calls' must be followed by tool
        #    messages responding to each 'tool_call_id' (insufficient tool
        #    messages following tool_calls message)"
        # Strategy: identify the tool messages we're about to drop, collect
        # the tool_call_ids that are orphaned, and strip those tool_calls
        # from the immediately-preceding assistant message.
        tool_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "tool"
        ]
        if not tool_indices:
            return messages, action, None
        drop_n = max(1, len(tool_indices) // 2)
        drop_set = set(tool_indices[:drop_n])
        # Collect tool_call_ids that would be orphaned
        orphaned_call_ids: set[str] = set()
        for i in drop_set:
            mm = messages[i]
            tid = mm.get("tool_call_id")
            if tid:
                orphaned_call_ids.add(tid)
        # Strip orphaned tool_calls from prior assistant messages
        new_msgs: list[dict] = []
        for i, m in enumerate(messages):
            if i in drop_set:
                continue
            if m.get("role") == "assistant" and m.get("tool_calls"):
                kept_tcs = [
                    tc for tc in m["tool_calls"]
                    if not (isinstance(tc, dict) and tc.get("id") in orphaned_call_ids)
                ]
                if len(kept_tcs) != len(m["tool_calls"]):
                    # Make a shallow copy with filtered tool_calls
                    m = {**m, "tool_calls": kept_tcs}
            new_msgs.append(m)
        return new_msgs, action, None

    if action == "fallback_to_different_model":
        # If we're already on the fallback model, escalate to noop.
        if (model or "").lower() == FALLBACK_MODEL_NAME:
            return messages, "noop", None
        return messages, action, FALLBACK_MODEL_NAME

    return messages, "noop", None
