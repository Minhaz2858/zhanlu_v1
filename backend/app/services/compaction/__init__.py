"""Conversation compaction system for Zhanlu — adapted from OpenHarness.

Three-layer progressive compaction:
- Microcompact: clear old tool result content (zero LLM cost)
- Session Memory: deterministic one-line summaries (zero LLM cost)
- Full Compact: LLM-generated structured summary

Also includes Context Collapse (head/tail truncation of oversized text blocks)
and PTL (Prompt Too Long) retry mechanism.

Adapted from OpenHarness's pure-async in-memory architecture to Zhanlu's
FastAPI + SQLAlchemy + SQLite stack. Works with Zhanlu's simple dict-based
message format instead of OpenHarness's ContentBlock objects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from .token_estimator import estimate_messages_tokens, estimate_text_tokens
from .microcompact import microcompact_messages
from .session_memory import try_session_memory_compaction
from .context_collapse import try_context_collapse, truncate_head_for_ptl_retry
from .full_compact import compact_conversation
from .attachments import (
    CompactAttachment,
    build_compact_attachments,
    render_compact_attachment,
)

log = logging.getLogger(__name__)

# Re-export for convenience
__all__ = [
    "CompactAttachment",
    "CompactionResult",
    "CompactionService",
    "AutoCompactState",
    "microcompact_messages",
    "try_session_memory_compaction",
    "try_context_collapse",
    "truncate_head_for_ptl_retry",
    "compact_conversation",
    "estimate_messages_tokens",
    "estimate_text_tokens",
    "should_autocompact",
    "get_autocompact_threshold",
    "auto_compact_if_needed",
    "build_post_compact_messages",
    "create_compact_boundary_message",
]

CompactTrigger = Literal["auto", "manual", "reactive"]
CompactionKind = Literal["full", "session_memory", "microcompact", "passthrough"]


@dataclass
class CompactionResult:
    """Structured compaction result."""

    trigger: CompactTrigger
    compact_kind: CompactionKind
    boundary_message: dict[str, Any]
    summary_messages: list[dict[str, Any]]
    messages_to_keep: list[dict[str, Any]]
    attachments: list[CompactAttachment] = field(default_factory=list)
    compact_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AutoCompactState:
    """Mutable state that persists across conversation turns."""

    compacted: bool = False
    turn_counter: int = 0
    consecutive_failures: int = 0


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTOCOMPACT_BUFFER_TOKENS = 13_000
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
COMPACT_TIMEOUT_SECONDS = 60
MAX_COMPACT_RETRIES = 2
MAX_PTL_RETRIES = 3
SESSION_MEMORY_KEEP_RECENT = 12
DEFAULT_PRESERVE_RECENT = 6
DEFAULT_CONTEXT_WINDOW = 32_000  # Conservative unknown-model default (any model must work)


def get_context_window(model: str, *, context_window_tokens: int | None = None) -> int:
    """Return the context window size for a model.

    Resolution order:
      1. explicit ``context_window_tokens`` override,
      2. the ``MODEL_CONTEXT_WINDOWS`` config map (per-model JSON, matched
         by substring so ``"qwen3.5-27b-tools"`` matches a ``"qwen3.5-27b"``
         entry) — this is how a locally-served model with a small
         ``num_ctx`` (e.g. Ollama's default 8192) gets its REAL budget
         instead of the 128k cloud default,
      3. name-based heuristics,
      4. ``DEFAULT_CONTEXT_WINDOW``.

    Bug fix: previously ``MODEL_CONTEXT_WINDOWS`` was declared in config
    but never read here, so a small local model always fell through to the
    128k default — autocompact/prune never fired and the report-synthesis
    LLM call overflowed the real context (empty automation reports).
    """
    if context_window_tokens is not None and context_window_tokens > 0:
        return int(context_window_tokens)
    m = (model or "").lower()
    # Auto-detected real window (probed from the endpoint's /v1/models or
    # /api/show at LLM resolution time).  Beats name heuristics because it
    # is the model's ACTUAL window — an unknown local model (Ollama 8k,
    # vLLM 32k, custom 16k) gets its true budget instead of a cloud
    # default that would overflow it.
    try:
        from app.services.context_probe import get_registered_context_window
        _probed = get_registered_context_window(model)
        if _probed and _probed > 0:
            return _probed
    except Exception:  # never let probe lookup break context resolution
        pass
    # Config-driven per-model override (substring match, longest key first
    # so a more specific model name wins over a generic family prefix).
    try:
        import json as _json
        from app.config import settings as _settings
        _raw = getattr(_settings, "MODEL_CONTEXT_WINDOWS", "") or ""
        if _raw:
            _table = _json.loads(_raw)
            if isinstance(_table, dict):
                for key in sorted(_table, key=len, reverse=True):
                    if key and key.lower() in m:
                        _val = int(_table[key])
                        if _val > 0:
                            return _val
    except Exception:  # never let config parsing break context resolution
        pass
    if "deepseek" in m:
        return 128_000
    if "gpt-4o" in m:
        return 128_000
    if "gpt-4" in m:
        return 8_000
    if "claude" in m or "sonnet" in m or "opus" in m or "haiku" in m:
        return 200_000
    # qwen3.6-27b is a local vLLM-served model with a 65,536-token context
    # window.  Without this override, the heuristic below returns 128,000
    # (the cloud qwen default), auto-compact never fires until 89,600
    # tokens, and the conversation overflows the model's REAL 65,536 limit
    # on the next turn -> 400 "maximum context length is 65536 tokens".
    # Keep more specific keys BEFORE the generic qwen fallback.
    if "qwen3.6-27b" in m and "awq4" not in m:
        return 65_536
    if "qwen" in m or "moonshot" in m or "kimi" in m:
        return 128_000
    return DEFAULT_CONTEXT_WINDOW


def get_autocompact_threshold(
    model: str,
    *,
    context_window_tokens: int | None = None,
    auto_compact_threshold_tokens: int | None = None,
) -> int:
    """Calculate the token count at which auto-compact fires."""
    if auto_compact_threshold_tokens is not None and auto_compact_threshold_tokens > 0:
        return int(auto_compact_threshold_tokens)
    context_window = get_context_window(model, context_window_tokens=context_window_tokens)
    # Scale the summary-reserve + buffer to the window. The flat 20k/13k
    # constants assume a large cloud context; on a small local model
    # (e.g. Ollama num_ctx=8192) they exceed the whole window and push the
    # threshold negative, so autocompact NEVER fired and the next LLM call
    # overflowed (empty report). Reserve ~25% for the summary output and a
    # ~15% safety buffer, floored so tiny windows still compact early.
    if context_window >= 64_000:
        reserved = min(MAX_OUTPUT_TOKENS_FOR_SUMMARY, 20_000)
        buffer = AUTOCOMPACT_BUFFER_TOKENS
    else:
        reserved = max(1_000, int(context_window * 0.25))
        buffer = max(500, int(context_window * 0.15))
    effective = context_window - reserved
    threshold = effective - buffer
    # Never let the threshold collapse to/below zero — compact once we cross
    # ~60% of a small window so there is still room for the summary + reply.
    return max(threshold, int(context_window * 0.5))


def should_autocompact(
    messages: list[dict[str, Any]],
    model: str,
    state: AutoCompactState,
    *,
    context_window_tokens: int | None = None,
    auto_compact_threshold_tokens: int | None = None,
) -> bool:
    """Return True when the conversation should be auto-compacted."""
    if state.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
        return False
    token_count = estimate_messages_tokens(messages)
    threshold = get_autocompact_threshold(
        model,
        context_window_tokens=context_window_tokens,
        auto_compact_threshold_tokens=auto_compact_threshold_tokens,
    )
    return token_count >= threshold


def create_compact_boundary_message(metadata: dict[str, Any]) -> dict[str, Any]:
    """Create a boundary marker message for post-compact conversation rebuild."""
    lines = [
        "[Compact boundary marker]",
        "Earlier conversation was compacted. Use the summary and preserved assets below as the continuity boundary.",
    ]
    trigger = str(metadata.get("trigger") or "").strip()
    compact_kind = str(metadata.get("compact_kind") or "").strip()
    pre_messages = metadata.get("pre_compact_message_count")
    pre_tokens = metadata.get("pre_compact_token_count")
    post_messages = metadata.get("post_compact_message_count")
    post_tokens = metadata.get("post_compact_token_count")
    if trigger:
        lines.append(f"Trigger: {trigger}")
    if compact_kind:
        lines.append(f"Compaction kind: {compact_kind}")
    if pre_messages is not None or pre_tokens is not None:
        lines.append(
            f"Pre-compact footprint: messages={pre_messages if pre_messages is not None else 'unknown'}, "
            f"tokens={pre_tokens if pre_tokens is not None else 'unknown'}"
        )
    if post_messages is not None or post_tokens is not None:
        lines.append(
            f"Post-compact footprint: messages={post_messages if post_messages is not None else 'unknown'}, "
            f"tokens={post_tokens if post_tokens is not None else 'unknown'}"
        )
    return {
        "role": "user",
        "content": "\n".join(lines),
        "_compact_boundary": True,
    }


def build_post_compact_messages(result: CompactionResult) -> list[dict[str, Any]]:
    """Rebuild the post-compact message list.

    NOTE: the caller is responsible for re-inserting the system prompt at
    index 0 (e.g. ``auto_compact_if_needed`` consumers in agents.py do
    ``if not messages or messages[0].get("role") != "system":
    messages.insert(0, {"role": "system", ...})``). This builder therefore
    MUST NOT emit a system-role message anywhere in the list: the boundary
    message (a ``user`` message) is first, and when a small conversation is
    kept verbatim (``messages_to_keep``), the original system prompt would
    otherwise land mid-list, producing two system messages after the
    caller's insert. vLLM/OpenAI reject that with "System message must be at
    the beginning."
    """
    attachment_messages = [render_compact_attachment(a) for a in result.attachments]
    kept = [
        result.boundary_message,
        *result.summary_messages,
        *result.messages_to_keep,
        *attachment_messages,
    ]
    if not kept:
        return []
    # Drop any system-role messages that slipped in via messages_to_keep.
    # If the first message IS system (never expected here, but be safe),
    # keep it at index 0; otherwise strip all system messages.
    if kept[0].get("role") == "system":
        return kept
    return [m for m in kept if m.get("role") != "system"]


class CompactionService:
    """Main compaction service — coordinates three-layer compression.

    Adapted from OpenHarness's auto_compact_if_needed() for Zhanlu's
    dict-based message format and llm_service.call_llm() API.
    """

    def __init__(
        self,
        *,
        model: str = "",
        context_window_tokens: int | None = None,
        auto_compact_threshold_tokens: int | None = None,
    ):
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.auto_compact_threshold_tokens = auto_compact_threshold_tokens
        self.state = AutoCompactState()

    async def auto_compact_if_needed(
        self,
        messages: list[dict[str, Any]],
        *,
        force: bool = False,
        trigger: CompactTrigger = "auto",
        carryover_metadata: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Check if auto-compact should fire, and if so, compact.

        Returns (messages, was_compacted).
        """
        if not force and not should_autocompact(
            messages,
            self.model,
            self.state,
            context_window_tokens=self.context_window_tokens,
            auto_compact_threshold_tokens=self.auto_compact_threshold_tokens,
        ):
            return messages, False

        log.info("Auto-compact triggered (failures=%d)", self.state.consecutive_failures)

        # Step 1: Try microcompact (cheap, zero LLM cost)
        messages, tokens_freed = microcompact_messages(messages)
        if tokens_freed > 0 and not should_autocompact(
            messages,
            self.model,
            self.state,
            context_window_tokens=self.context_window_tokens,
            auto_compact_threshold_tokens=self.auto_compact_threshold_tokens,
        ):
            log.info("Microcompact freed ~%d tokens, auto-compact no longer needed", tokens_freed)
            self.state.compacted = True
            self.state.consecutive_failures = 0
            return messages, True

        # Step 2: Try context collapse (deterministic, zero LLM cost)
        collapsed = try_context_collapse(messages, preserve_recent=DEFAULT_PRESERVE_RECENT)
        if collapsed is not None:
            messages = collapsed
            if not force and not should_autocompact(
                messages,
                self.model,
                self.state,
                context_window_tokens=self.context_window_tokens,
                auto_compact_threshold_tokens=self.auto_compact_threshold_tokens,
            ):
                return messages, True

        # Step 3: Try session memory compaction (deterministic, zero LLM cost)
        session_result = try_session_memory_compaction(
            messages,
            preserve_recent=max(DEFAULT_PRESERVE_RECENT, SESSION_MEMORY_KEEP_RECENT),
            trigger=trigger,
            metadata=carryover_metadata,
        )
        if session_result is not None:
            self.state.compacted = True
            self.state.turn_counter += 1
            self.state.consecutive_failures = 0
            return build_post_compact_messages(session_result), True

        # Step 4: Full compact (LLM-based)
        try:
            result = await compact_conversation(
                messages,
                model=self.model,
                preserve_recent=DEFAULT_PRESERVE_RECENT,
                trigger=trigger,
                carryover_metadata=carryover_metadata,
            )
            self.state.compacted = True
            self.state.turn_counter += 1
            self.state.consecutive_failures = 0
            return build_post_compact_messages(result), True
        except Exception as exc:
            self.state.consecutive_failures += 1
            log.error(
                "Auto-compact failed (attempt %d/%d): %s",
                self.state.consecutive_failures,
                MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
                exc,
            )
            return messages, False


async def auto_compact_if_needed(
    messages: list[dict[str, Any]],
    *,
    model: str = "",
    state: AutoCompactState | None = None,
    context_window_tokens: int | None = None,
    auto_compact_threshold_tokens: int | None = None,
    force: bool = False,
    trigger: CompactTrigger = "auto",
    carryover_metadata: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Module-level convenience function — create a CompactionService and run."""
    service = CompactionService(
        model=model,
        context_window_tokens=context_window_tokens,
        auto_compact_threshold_tokens=auto_compact_threshold_tokens,
    )
    if state is not None:
        service.state = state
    return await service.auto_compact_if_needed(
        messages,
        force=force,
        trigger=trigger,
        carryover_metadata=carryover_metadata,
    )
