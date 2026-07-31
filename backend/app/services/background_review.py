"""Background memory/skill review — async post-turn self-improvement.

After every N turns, spawns an asyncio background task that replays the
conversation snapshot and asks the LLM "should any memory be saved or
updated from this turn?". The task uses the ``memory`` tool only — no
other tools are available. Writes go straight to the memory store.

The main conversation is never touched. The task is fire-and-forget:
failures are logged but never surface to the user as errors.

Inspired by Hermes' ``agent/background_review.py``, adapted for Zhanlu's
async FastAPI architecture (asyncio tasks instead of daemon threads,
settings-based config instead of credential pools).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Run background review every N turns (not every turn — too expensive).
DEFAULT_REVIEW_INTERVAL = 5

# Max iterations for the review agent (keep it short — it's a quick scan).
_REVIEW_MAX_ITERATIONS = 3

_MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n"
    "3. Were there any important decisions, constraints, or facts established that "
    "would be useful in future conversations?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop.\n\n"
    "Do NOT capture:\n"
    "  - Environment-dependent failures (missing binaries, 'command not found').\n"
    "  - Negative claims about tools ('X tool is broken').\n"
    "  - One-off task narratives that aren't reusable knowledge.\n"
    "  - Session-specific transient errors that resolved before the conversation ended.\n"
)


def _digest_history(messages: list[dict], tail: int = 20) -> list[dict]:
    """Compact replay for the review — keep recent messages, summarize older ones.

    Keeps the recent ``tail`` messages verbatim, collapses older turns into
    one synthetic user-role digest. Preserves role alternation.
    """
    msgs = list(messages or [])
    if len(msgs) <= tail:
        return msgs

    keep = msgs[-tail:]
    # Ensure we don't start with a tool message (role alternation)
    while keep and isinstance(keep[0], dict) and keep[0].get("role") == "tool":
        tail += 1
        if len(msgs) <= tail:
            return msgs
        keep = msgs[-tail:]

    old = msgs[:-len(keep)]
    lines: list[str] = []
    for m in old:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content", "")
        if not isinstance(content, str):
            content = ""
        text = content.strip().replace("\n", " ")
        if role == "user" and text:
            lines.append(f"USER: {text[:300]}")
        elif role == "assistant":
            tcs = m.get("tool_calls") or []
            if tcs:
                names = [
                    (tc.get("function") or {}).get("name", "?")
                    for tc in tcs if isinstance(tc, dict)
                ]
                lines.append(f"ASSISTANT[tools: {', '.join(names)}]")
            if text:
                lines.append(f"ASSISTANT: {text[:200]}")

    digest = {
        "role": "user",
        "content": (
            "[Earlier conversation digest — older turns summarised. "
            "Recent turns follow verbatim below.]\n" + "\n".join(lines)
        ),
    }
    return [digest] + keep


async def _run_background_review(
    conversation_id: str,
    messages_snapshot: list[dict],
    model: str | None,
    api_key: str | None,
    base_url: str | None,
    memory_tool_schema: list[dict] | None,
) -> None:
    """Worker coroutine for the background review.

    Calls the LLM with the conversation snapshot + review prompt, with only
    the ``memory`` tool available. Any memory writes the LLM makes go to the
    memory store via the tool execution path.
    """
    if not messages_snapshot:
        return

    try:
        from openai import AsyncOpenAI
        from app.services.agent_tools import execute_tool_with_retry
        from app.config import settings
    except ImportError as e:
        logger.debug("Background review skipped — missing dependency: %s", e)
        return

    # Build the review messages
    review_messages = _digest_history(messages_snapshot)
    review_messages.append({
        "role": "user",
        "content": _MEMORY_REVIEW_PROMPT,
    })

    # Use the memory tool only
    tools = memory_tool_schema or []
    if not tools:
        logger.debug("Background review skipped — no memory tool schema available")
        return

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    used_model = model or getattr(settings, "LLM_MODEL", "deepseek-chat")

    try:
        for _iteration in range(_REVIEW_MAX_ITERATIONS):
            response = await client.chat.completions.create(
                model=used_model,
                messages=review_messages,
                tools=tools,
                tool_choice="auto" if tools else None,
                max_tokens=1024,
                temperature=0.3,
            )

            choice = response.choices[0]
            msg = choice.message

            # Append the assistant response
            review_messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in (msg.tool_calls or [])
                ] if msg.tool_calls else None,
            })

            # No tool calls -> review is done
            if not msg.tool_calls:
                content = (msg.content or "").strip()
                if content and content != "Nothing to save.":
                    logger.info("Background review completed for %s: %s", conversation_id, content[:200])
                else:
                    logger.debug("Background review: nothing to save for %s", conversation_id)

                # P5: Run memory consolidation after the review
                try:
                    from app.services.memory_manager import run_consolidation
                    from app.database import AsyncSessionLocal
                    from app.models.agent_app import AgentApp
                    async with AsyncSessionLocal() as consolidate_db:
                        # Find the agent_app_id for this conversation
                        from sqlalchemy import select
                        from app.models.conversation import Conversation
                        conv = await consolidate_db.execute(
                            select(Conversation).where(Conversation.id == conversation_id)
                        )
                        conv_row = conv.scalar_one_or_none()
                        if conv_row and conv_row.agent_app_id:
                            report = run_consolidation(
                                consolidate_db, conv_row.agent_app_id
                            )
                            if report.total_before != report.total_after:
                                logger.info(
                                    "Memory consolidation for %s: %d -> %d memories",
                                    conversation_id, report.total_before, report.total_after,
                                )
                except Exception as e:
                    logger.debug("Memory consolidation skipped (non-fatal): %s", e)

                return

            # Execute memory tool calls
            import os
            # Get a DB session for tool execution
            try:
                from app.database import AsyncSessionLocal
                async with AsyncSessionLocal() as db:
                    for tc in msg.tool_calls:
                        tool_name = tc.function.name
                        try:
                            args = json.loads(tc.function.arguments)
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                        try:
                            result = await execute_tool_with_retry(
                                tool_name, args, db,
                                user_id=None,
                                context={"conversation_id": conversation_id},
                            )
                            review_messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": json.dumps(result, ensure_ascii=False, default=str),
                            })
                        except Exception as e:
                            logger.warning("Background review tool execution failed: %s", e)
                            review_messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": json.dumps({"success": False, "error": str(e)}),
                            })
            except Exception as e:
                logger.warning("Background review DB session failed: %s", e)
                return

    except asyncio.CancelledError:
        logger.debug("Background review cancelled for %s", conversation_id)
        raise
    except Exception as e:
        logger.warning("Background review failed for %s: %s", conversation_id, e)


def spawn_background_review(
    conversation_id: str,
    messages_snapshot: list[dict],
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    memory_tool_schema: list[dict] | None = None,
) -> asyncio.Task | None:
    """Spawn a fire-and-forget background review task.

    Returns the asyncio Task (or None if no event loop is running).
    The task is detached — failures are logged but never surface as errors.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("Background review skipped — no running event loop")
        return None

    task = loop.create_task(
        _run_background_review(
            conversation_id,
            messages_snapshot,
            model,
            api_key,
            base_url,
            memory_tool_schema,
        ),
        name=f"bg-review-{conversation_id}",
    )

    # Detach — log exceptions but don't propagate
    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.warning("Background review task error: %s", exc)

    task.add_done_callback(_on_done)
    return task


__all__ = [
    "spawn_background_review",
    "DEFAULT_REVIEW_INTERVAL",
]
