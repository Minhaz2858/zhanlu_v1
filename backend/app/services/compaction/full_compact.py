"""Full Compact — LLM-based structured summarization.

This is the most expensive compaction layer: it calls the LLM to produce
a structured summary of older messages. Uses Zhanlu's llm_service.call_llm()
with PTL (Prompt Too Long) retry support.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .token_estimator import estimate_messages_tokens
from .microcompact import microcompact_messages, DEFAULT_KEEP_RECENT
from .context_collapse import truncate_head_for_ptl_retry
from .attachments import build_compact_attachments, render_compact_attachment

log = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
COMPACT_TIMEOUT_SECONDS = 120
MAX_COMPACT_RETRIES = 2
MAX_PTL_RETRIES = 3
DEFAULT_PRESERVE_RECENT = 6
ERROR_MESSAGE_INCOMPLETE_RESPONSE = "Compaction interrupted before a complete summary was returned."

NO_TOOLS_PREAMBLE = """\
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use read_file, write_file, web_search, execute_code, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

"""

BASE_COMPACT_PROMPT = """\
Your task is to create a detailed summary of the conversation so far. This summary will replace the earlier messages, so it must capture all important information.

First, draft your analysis inside <analysis> tags. Walk through the conversation chronologically and extract:
- Every user request and intent (explicit and implicit)
- The approach taken and technical decisions made
- Specific code, files, and configurations discussed (with paths and line numbers where available)
- All errors encountered and how they were fixed
- Any user feedback or corrections

Then, produce a structured summary inside <summary> tags with these sections:

1. **Primary Request and Intent**: All user requests in full detail, including nuances and constraints.
2. **Key Technical Concepts**: Technologies, frameworks, patterns, and conventions discussed.
3. **Files and Code Sections**: Every file examined or modified, with specific code snippets and line numbers.
4. **Errors and Fixes**: Every error encountered, its cause, and how it was resolved.
5. **Problem Solving**: Problems solved and approaches that worked vs. didn't work.
6. **All User Messages**: Non-tool-result user messages (preserve exact wording for context).
7. **Pending Tasks**: Explicitly requested work that hasn't been completed yet.
8. **Current Work**: Detailed description of the last task being worked on before compaction.
9. **Optional Next Step**: The single most logical next step, directly aligned with the user's recent request.
"""

NO_TOOLS_TRAILER = """
REMINDER: Do NOT call any tools. Respond with plain text only — an <analysis> block followed by a <summary> block. Tool calls will be rejected and you will fail the task."""


def get_compact_prompt(custom_instructions: str | None = None) -> str:
    prompt = NO_TOOLS_PREAMBLE + BASE_COMPACT_PROMPT
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions}"
    prompt += NO_TOOLS_TRAILER
    return prompt


def format_compact_summary(raw_summary: str) -> str:
    text = re.sub(r"<analysis>[\s\S]*?</analysis>", "", raw_summary)
    m = re.search(r"<summary>([\s\S]*?)</summary>", text)
    if m:
        text = text.replace(m.group(0), f"Summary:\n{m.group(1).strip()}")
    text = re.sub(r"\n\n+", "\n\n", text)
    return text.strip()


def build_compact_summary_message(
    summary: str,
    *,
    suppress_follow_up: bool = False,
    recent_preserved: bool = False,
) -> str:
    formatted = format_compact_summary(summary)
    text = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
        "into the summary below. This is a handoff from a previous context "
        "window — treat it as background reference, NOT as active "
        "instructions. Do NOT answer questions or fulfill requests mentioned "
        "in this summary; they were already addressed. Respond ONLY to the "
        "latest user message that appears AFTER this summary — that message "
        "is the single source of truth for what to do right now.\n\n"
        "This session is being continued from a previous conversation that ran "
        "out of context. The summary below covers the earlier portion of the "
        "conversation.\n\n"
        f"{formatted}"
        "\n\n"
        "IMPORTANT — compaction guardrails:\n"
        "1. Reverse signals in the latest message (e.g. 'stop', 'undo', 'roll "
        "back', 'just verify', 'don't do that anymore', 'never mind', or a "
        "new topic) must immediately end any in-flight work described in the "
        "summary above; do not re-surface it in later turns.\n"
        "2. Your persistent memory (memory tool entries, USER preferences) is "
        "ALWAYS authoritative and active — never ignore or deprioritize "
        "memory content due to this compaction note.\n"
        "3. If the summary mentions pending tasks, only continue them when the "
        "latest user message explicitly asks you to."
    )
    if recent_preserved:
        text += "\n\nRecent messages are preserved verbatim."
    if suppress_follow_up:
        text += (
            "\nContinue the conversation from where it left off without asking "
            "the user any further questions. Resume directly."
        )
    return text


def _is_prompt_too_long_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        needle in text
        for needle in (
            "prompt too long",
            "context_length_exceeded",
            "context length",
            "maximum context",
            "context window",
            "input tokens exceed",
            "reduce the length of the messages",
            "too many tokens",
            "too large for the model",
            "maximum context length",
            "exceed_context",
            "exceeds the available context size",
        )
    )


def _split_preserving_tool_pairs(
    messages: list[dict[str, Any]],
    *,
    preserve_recent: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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


async def _call_llm_for_summary(
    messages_to_summarize: list[dict[str, Any]],
    system_prompt: str,
    model: str,
) -> str:
    from app.services.llm_service import call_llm

    compact_prompt = get_compact_prompt()
    compact_messages = [*messages_to_summarize, {"role": "user", "content": compact_prompt}]

    result = await call_llm(
        messages=compact_messages,
        temperature=0.3,
    )

    summary_text = result.get("response", "")
    if not summary_text.strip():
        raise RuntimeError(ERROR_MESSAGE_INCOMPLETE_RESPONSE)
    return summary_text


async def compact_conversation(
    messages: list[dict[str, Any]],
    *,
    model: str = "",
    system_prompt: str = "",
    preserve_recent: int = DEFAULT_PRESERVE_RECENT,
    custom_instructions: str | None = None,
    suppress_follow_up: bool = True,
    trigger: str = "manual",
    carryover_metadata: dict[str, Any] | None = None,
) -> "CompactionResult":
    """Compact messages by calling the LLM to produce a summary."""
    from . import CompactionResult, create_compact_boundary_message, build_post_compact_messages

    if len(messages) <= preserve_recent:
        compact_metadata = {
            "trigger": trigger,
            "compact_kind": "full",
            "pre_compact_message_count": len(messages),
            "pre_compact_token_count": estimate_messages_tokens(messages),
            "reason": "conversation already within preserve_recent window",
        }
        result = CompactionResult(
            trigger=trigger,
            compact_kind="full",
            boundary_message=create_compact_boundary_message(compact_metadata),
            summary_messages=[],
            messages_to_keep=list(messages),
            attachments=[],
            compact_metadata=compact_metadata,
        )
        post = build_post_compact_messages(result)
        result.compact_metadata["post_compact_message_count"] = len(post)
        result.compact_metadata["post_compact_token_count"] = estimate_messages_tokens(post)
        result.boundary_message = create_compact_boundary_message(result.compact_metadata)
        return result

    # Step 1: microcompact
    messages, tokens_freed = microcompact_messages(messages, keep_recent=DEFAULT_KEEP_RECENT)
    pre_compact_tokens = estimate_messages_tokens(messages)
    log.info("Compacting conversation: %d messages, ~%d tokens", len(messages), pre_compact_tokens)

    # Step 2: split
    older, newer = _split_preserving_tool_pairs(messages, preserve_recent=preserve_recent)

    if not older:
        compact_metadata = {
            "trigger": trigger,
            "compact_kind": "full",
            "pre_compact_message_count": len(messages),
            "pre_compact_token_count": pre_compact_tokens,
            "reason": "no older messages to compact",
        }
        result = CompactionResult(
            trigger=trigger,
            compact_kind="full",
            boundary_message=create_compact_boundary_message(compact_metadata),
            summary_messages=[],
            messages_to_keep=list(newer),
            attachments=[],
            compact_metadata=compact_metadata,
        )
        post = build_post_compact_messages(result)
        result.compact_metadata["post_compact_message_count"] = len(post)
        result.compact_metadata["post_compact_token_count"] = estimate_messages_tokens(post)
        result.boundary_message = create_compact_boundary_message(result.compact_metadata)
        return result

    # Step 3: call LLM with PTL retry
    summary_text = ""
    retry_messages = list(older)
    ptl_retries = 0

    for attempt in range(1, MAX_COMPACT_RETRIES + 2):
        try:
            summary_text = await asyncio.wait_for(
                _call_llm_for_summary(retry_messages, system_prompt or "You are a conversation summarizer.", model),
                timeout=COMPACT_TIMEOUT_SECONDS,
            )
            break
        except Exception as exc:
            if _is_prompt_too_long_error(exc) and ptl_retries < MAX_PTL_RETRIES:
                truncated = truncate_head_for_ptl_retry(retry_messages)
                if truncated:
                    ptl_retries += 1
                    retry_messages = truncated
                    log.info("PTL retry %d: truncated to %d messages", ptl_retries, len(retry_messages))
                    continue
            if attempt > MAX_COMPACT_RETRIES:
                log.error("Compaction failed after %d attempts: %s", attempt, exc)
                raise
            log.warning("Compaction attempt %d failed: %s, retrying...", attempt, exc)

    if not summary_text:
        log.warning("Compact summary was empty — returning original messages")
        compact_metadata = {
            "trigger": trigger,
            "compact_kind": "full",
            "pre_compact_message_count": len(messages),
            "pre_compact_token_count": pre_compact_tokens,
            "reason": ERROR_MESSAGE_INCOMPLETE_RESPONSE,
        }
        result = CompactionResult(
            trigger=trigger,
            compact_kind="full",
            boundary_message=create_compact_boundary_message(compact_metadata),
            summary_messages=[],
            messages_to_keep=list(messages),
            attachments=[],
            compact_metadata=compact_metadata,
        )
        post = build_post_compact_messages(result)
        result.compact_metadata["post_compact_message_count"] = len(post)
        result.compact_metadata["post_compact_token_count"] = estimate_messages_tokens(post)
        result.boundary_message = create_compact_boundary_message(result.compact_metadata)
        return result

    # Step 4: build result
    summary_content = build_compact_summary_message(
        summary_text,
        suppress_follow_up=suppress_follow_up,
        recent_preserved=len(newer) > 0,
    )
    summary_msg = {"role": "user", "content": summary_content}

    compact_metadata = {
        "trigger": trigger,
        "compact_kind": "full",
        "pre_compact_message_count": len(messages),
        "pre_compact_token_count": pre_compact_tokens,
        "preserve_recent": preserve_recent,
        "tokens_freed_by_microcompact": tokens_freed,
        "used_head_truncation_retry": ptl_retries > 0,
        "retry_attempts": max(0, attempt - 1 if "attempt" in locals() else 0),
    }

    compaction_result = CompactionResult(
        trigger=trigger,
        compact_kind="full",
        boundary_message=create_compact_boundary_message(compact_metadata),
        summary_messages=[summary_msg],
        messages_to_keep=list(newer),
        attachments=build_compact_attachments(older, metadata=carryover_metadata),
        compact_metadata=compact_metadata,
    )

    post_compact_messages = build_post_compact_messages(compaction_result)
    post_compact_tokens = estimate_messages_tokens(post_compact_messages)
    compaction_result.compact_metadata["post_compact_message_count"] = len(post_compact_messages)
    compaction_result.compact_metadata["post_compact_token_count"] = post_compact_tokens
    compaction_result.boundary_message = create_compact_boundary_message(compaction_result.compact_metadata)

    log.info(
        "Compaction done: %d -> %d messages, ~%d -> ~%d tokens (saved ~%d)",
        len(messages), len(post_compact_messages),
        pre_compact_tokens, post_compact_tokens,
        pre_compact_tokens - post_compact_tokens,
    )

    return compaction_result
