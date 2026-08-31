"""Hermes-style proactive FSM-state pruning for the v3 agent loop.

Implements two deterministic, no-LLM techniques inspired by Hermes Agent:

1. **Hard tool output caps** (Step 1): Cap individual tool results at
   ``FSM_PRUNE_MIN_RESULT_CHARS`` (~50k chars ≈ 12-15k tokens). Keeps
   the first 40% + last 60% with a truncation marker.

2. **Proactive pruning between FSM states** (Step 2): Replace old tool
   results with a compact state checkpoint. This prevents accumulation
   of 50k+ tokens of tool results across multiple data fetches in the
   v3 FSM loop.

Performance: sub-millisecond, O(n) list operations, zero LLM calls,
zero VRAM spike. Ideal for local inference (qwen3.6-27b on SSH server).
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Hermes-style thresholds (tuned for local inference)
FSM_PRUNE_MIN_RESULT_CHARS = 50_000  # ~12-15k tokens — hard cap per tool result
FSM_PRUNE_MAX_TOTAL_CHARS = 40_000   # ~10k tokens — total tool results budget per turn
FSM_PRUNE_KEEP_RECENT = 1            # Keep the latest N tool results verbatim


def hard_cap_tool_result(result: Any, max_chars: int = FSM_PRUNE_MIN_RESULT_CHARS) -> Any:
    """Cap a tool result to ``max_chars`` using Hermes-style 40/60 split.

    If the result is a dict/list, serialize to JSON first, cap, then try
    to re-parse. If re-parsing fails, return the capped string.

    Keeps the first 40% + last 60% with a truncation marker so the LLM
    can see the result was elided, not just cut off mid-sentence.
    """
    if result is None:
        return result

    result_str = json.dumps(result, ensure_ascii=False, default=str)
    if len(result_str) <= max_chars:
        return result

    keep_start = int(max_chars * 0.4)
    keep_end = max_chars - keep_start - 100  # reserve for marker
    marker = f"\n[...OUTPUT TRUNCATED to {max_chars} chars ({len(result_str)} total)...]\n"

    capped = result_str[:keep_start] + marker + result_str[-keep_end:]

    # Try to re-parse as JSON; if it fails, return the capped string
    try:
        return json.loads(capped)
    except json.JSONDecodeError:
        return capped


def prune_between_fsm_states(
    messages: list[dict],
    current_state: str,
    *,
    keep_recent: int = FSM_PRUNE_KEEP_RECENT,
    max_total_chars: int = FSM_PRUNE_MAX_TOTAL_CHARS,
) -> list[dict]:
    """Replace old tool results with a compact state checkpoint.

    Called between FSM iterations in the v3 stream loop. Replaces all but
    the ``keep_recent`` most recent tool results with a single assistant
    checkpoint message containing metadata about what was replaced.

    Returns a NEW list (does not mutate the input).
    """
    if not messages or len(messages) <= 3:
        return messages

    # Find tool message indices
    tool_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool"
    ]

    if len(tool_indices) <= keep_recent:
        return messages  # Nothing to prune

    # Calculate total size of tool results
    total_tool_chars = sum(
        len(str(messages[i].get("content", ""))) for i in tool_indices
    )

    # Only prune if we're over budget
    if total_tool_chars <= max_total_chars:
        return messages

    # Split: keep the latest ``keep_recent`` tool results, replace the rest
    indices_to_replace = tool_indices[:-keep_recent]
    indices_to_keep = set(tool_indices[-keep_recent:])

    # Build checkpoint message
    replaced_count = len(indices_to_replace)
    replaced_chars = sum(
        len(str(messages[i].get("content", ""))) for i in indices_to_replace
    )
    checkpoint_content = (
        f"[Checkpoint: {current_state}] "
        f"{replaced_count} prior data fetch(es) summarized "
        f"({replaced_chars:,} chars reclaimed). "
        f"Latest result preserved for analysis."
    )

    # Collect tool_call_ids that would be orphaned by dropping these tool
    # messages. DeepSeek/OpenAI reject an assistant message whose
    # tool_calls have no following tool response:
    #   "An assistant message with 'tool_calls' must be followed by tool
    #    messages responding to each 'tool_call_id' (insufficient tool
    #    messages following tool_calls message)"
    # The prior assistant messages REMAIN in the list, so we must strip
    # the orphaned calls from them (same pattern as
    # pre_api_prune.escalate()'s drop_old_tool_messages, 2026-08-25).
    orphaned_call_ids: set[str] = set()
    for i in indices_to_replace:
        tid = messages[i].get("tool_call_id")
        if tid:
            orphaned_call_ids.add(tid)

    # 2026-08-29 BUGFIX: the checkpoint message must be inserted AFTER the
    # contiguous run of tool messages that starts at the first replaced
    # index — NOT at the first replaced index itself. The kept (most
    # recent) tool responses live at the END of that run, and their
    # assistant(tool_calls) message must be IMMEDIATELY followed by them.
    # Inserting the checkpoint between the assistant and its tool
    # responses produced exactly the 400 this pruner exists to prevent:
    #   "An assistant message with 'tool_calls' must be followed by tool
    #    messages responding to each 'tool_call_id'"
    # (reproduced live: conversation 8e749a1e, "sales sanpshoot" follow-up).
    first_replaced = indices_to_replace[0]
    insert_at = first_replaced
    while insert_at < len(messages) and messages[insert_at].get("role") == "tool":
        insert_at += 1

    # Build new message list
    out: list[dict] = []
    checkpoint_inserted = False

    for i, msg in enumerate(messages):
        if i in indices_to_replace:
            continue
        if not checkpoint_inserted and i == insert_at:
            out.append({
                "role": "assistant",
                "content": checkpoint_content,
            })
            checkpoint_inserted = True
        if (
            msg.get("role") == "assistant"
            and msg.get("tool_calls")
            and orphaned_call_ids
        ):
            kept_tcs = [
                tc for tc in msg["tool_calls"]
                if not (isinstance(tc, dict) and tc.get("id") in orphaned_call_ids)
            ]
            if len(kept_tcs) != len(msg["tool_calls"]):
                # Shallow copy with the orphaned calls removed; keep any
                # visible text. A fully-orphaned assistant message with no
                # text is dropped. When all calls are stripped, the
                # tool_calls KEY must go too — DeepSeek rejects
                # `tool_calls: []` with "Invalid 'messages[2].tool_calls':
                # empty array" (400).
                if not kept_tcs:
                    if not msg.get("content"):
                        continue
                    msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                else:
                    msg = {**msg, "tool_calls": kept_tcs}
        out.append(msg)

    kept_chars = sum(
        len(str(messages[i].get("content", ""))) for i in indices_to_keep
    )
    logger.info(
        "FSM pruner: replaced %d old tool results (%d chars) with checkpoint; "
        "kept %d recent (%d chars total)",
        replaced_count, replaced_chars, keep_recent, kept_chars,
    )

    return out


__all__ = [
    "hard_cap_tool_result",
    "prune_between_fsm_states",
    "FSM_PRUNE_MIN_RESULT_CHARS",
    "FSM_PRUNE_MAX_TOTAL_CHARS",
    "FSM_PRUNE_KEEP_RECENT",
]
