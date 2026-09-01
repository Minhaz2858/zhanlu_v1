"""3-layer tool result persistence for context overflow protection.

Layer 1: per-tool cap (truncation) -- handled by tool_security.truncate_output.
Layer 2: per-result persistence -- write large results to disk, replace inline
         with a preview + a pointer the LLM can use to read the full result.
Layer 3: per-turn aggregate budget -- if the total output of all tools in a
         single turn exceeds the budget, spill the largest results to disk.

Inspired by Hermes' ``tools/tool_result_storage.py`` + ``tools/budget_config.py``,
adapted for Zhanlu's file-based persistence.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)

# Tools whose results must NEVER be persisted to disk. read_file is the
# critical one: persisting its output creates a read->persist->read loop
# where the LLM reads the persisted file, which gets persisted again.
PINNED_NO_PERSIST: frozenset[str] = frozenset({
    "read_file",
})

# Default budget values (chars). Scaled to context window in budget_for_context.
DEFAULT_RESULT_THRESHOLD: int = 20_000
DEFAULT_TURN_BUDGET: int = 80_000
DEFAULT_PREVIEW_CHARS: int = 1_500

# Floor so tiny models still get usable previews.
_MIN_RESULT_THRESHOLD: int = 4_000
_MIN_TURN_BUDGET: int = 16_000

# Token<->char ratio (conservative, matches token_estimator.py).
_CHARS_PER_TOKEN: int = 4


@dataclass(frozen=True)
class PersistenceConfig:
    """Budget constants for the 3-layer persistence system.

    Attributes:
        result_threshold_chars: Layer 2 threshold -- results larger than this
            get persisted to disk with an inline preview.
        turn_budget_chars: Layer 3 threshold -- if total turn output exceeds
            this, largest results get spilled to disk.
        preview_chars: Size of the inline preview after persistence.
        no_persist_tools: Tools whose results must never be persisted (e.g.
            read_file, to prevent persist->read->persist loops).
    """
    result_threshold_chars: int = DEFAULT_RESULT_THRESHOLD
    turn_budget_chars: int = DEFAULT_TURN_BUDGET
    preview_chars: int = DEFAULT_PREVIEW_CHARS
    no_persist_tools: frozenset[str] = frozenset(PINNED_NO_PERSIST)


def budget_for_context_window(context_length: int | None) -> PersistenceConfig:
    """Return a PersistenceConfig scaled to the model's context window.

    Large models (200K+ tokens) get the default budget. Smaller models get
    proportionally smaller budgets, floored so previews remain usable.

    Args:
        context_length: The model's max context length in tokens.

    Returns:
        A PersistenceConfig with thresholds clamped to [min, default].
    """
    if not context_length or context_length <= 0:
        return PersistenceConfig()

    window_chars = context_length * _CHARS_PER_TOKEN
    per_result = int(window_chars * 0.10)   # 10% of window per single result
    per_turn = int(window_chars * 0.25)     # 25% of window per turn total

    per_result = max(_MIN_RESULT_THRESHOLD, min(per_result, DEFAULT_RESULT_THRESHOLD))
    per_turn = max(_MIN_TURN_BUDGET, min(per_turn, DEFAULT_TURN_BUDGET))

    return PersistenceConfig(
        result_threshold_chars=per_result,
        turn_budget_chars=per_turn,
        preview_chars=DEFAULT_PREVIEW_CHARS,
    )


def persist_tool_result(
    tool_name: str,
    result_str: str,
    storage_dir: str,
    config: PersistenceConfig | None = None,
    conversation_id: str | None = None,
    *,
    force: bool = False,
) -> tuple[str, dict]:
    """Layer 2: persist a large tool result to disk, return a preview string.

    If the result is under the threshold, or the tool is in the no-persist
    set, the original string is returned unchanged.

    Args:
        tool_name: Name of the tool that produced the result.
        result_str: The JSON-serialized tool result string.
        storage_dir: Directory to write persisted results.
        config: Persistence config (uses defaults if None).
        conversation_id: Optional conversation ID for filename namespacing.
        force: If True, bypass the per-result threshold check (used by Layer 3
            turn-budget spill to persist results that are individually small
            but collectively overflow the turn budget).

    Returns:
        ``(new_result_str, metadata)`` where metadata has keys:
        - ``persisted``: bool
        - ``stored_path``: str (only if persisted)
        - ``original_size``: int
    """
    config = config or PersistenceConfig()
    metadata: dict = {"persisted": False, "original_size": len(result_str)}

    if tool_name in config.no_persist_tools:
        return result_str, metadata

    if not force and len(result_str) <= config.result_threshold_chars:
        return result_str, metadata

    # Write to disk
    os.makedirs(storage_dir, exist_ok=True)
    file_id = uuid.uuid4().hex[:12]
    conv_prefix = (conversation_id or "conv")[:8]
    filename = f"toolresult_{conv_prefix}_{tool_name}_{file_id}.json"
    filepath = os.path.join(storage_dir, filename)

    try:
        with open(filepath, "w", encoding="utf-8", errors="surrogatepass") as f:
            f.write(result_str)
    except OSError as e:
        logger.warning("Failed to persist tool result for %s: %s", tool_name, e)
        return result_str, metadata

    # Build preview: first N chars + pointer to full result
    preview = result_str[:config.preview_chars]
    pointer = (
        f"\n...[truncated. Full result at: {filepath}]"
    )
    new_str = preview + pointer

    # Guard: if the preview is larger than the original (small results under
    # force=True from Layer 3), abandon persistence and return the original.
    if len(new_str) >= len(result_str):
        try:
            os.remove(filepath)
        except OSError:
            pass
        return result_str, metadata

    metadata["persisted"] = True
    metadata["stored_path"] = filepath
    logger.info(
        "Persisted tool result for '%s': %d chars -> disk (%s), inline preview %d chars",
        tool_name, len(result_str), filepath, len(new_str),
    )
    return new_str, metadata


def apply_turn_budget(
    results: Sequence[tuple[str, str]],
    storage_dir: str,
    config: PersistenceConfig | None = None,
    conversation_id: str | None = None,
) -> list[tuple[str, str]]:
    """Layer 3: if total turn output exceeds budget, spill largest results to disk.

    Args:
        results: List of ``(tool_name, result_str)`` tuples for one turn.
        storage_dir: Directory to write spilled results.
        config: Persistence config (uses defaults if None).
        conversation_id: Optional conversation ID for filename namespacing.

    Returns:
        List of ``(tool_name, new_result_str)`` tuples, where some may have
        been replaced with previews.
    """
    config = config or PersistenceConfig()

    total_chars = sum(len(r) for _, r in results)
    if total_chars <= config.turn_budget_chars:
        return list(results)

    # Sort by size descending -- spill largest first
    indexed = list(enumerate(results))
    indexed.sort(key=lambda x: len(x[1][1]), reverse=True)

    output: dict[int, tuple[str, str]] = {}
    current_total = total_chars
    spilled = set()

    for idx, (tool_name, result_str) in indexed:
        if current_total <= config.turn_budget_chars:
            output[idx] = (tool_name, result_str)
            continue
        # Don't spill no-persist tools (read_file) -- they'd loop
        if tool_name in config.no_persist_tools:
            output[idx] = (tool_name, result_str)
            continue
        new_str, meta = persist_tool_result(
            tool_name, result_str, storage_dir, config, conversation_id,
            force=True,
        )
        if meta["persisted"]:
            current_total -= len(result_str) - len(new_str)
            spilled.add(idx)
        output[idx] = (tool_name, new_str)

    result_list = [output[i] for i in range(len(results))]
    if spilled:
        logger.info(
            "Turn budget spill: %d/%d results persisted to disk (total %d -> %d chars)",
            len(spilled), len(results), total_chars, sum(len(r) for _, r in result_list),
        )
    return result_list
