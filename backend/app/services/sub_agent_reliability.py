"""Reliability helpers for sub-agent LLM/tool loops.

Wires the same P0-P4 reliability features used by the main agent turn loop
(``app.routers.agents``) into the two sub-agent code paths:

* ``delegate_task`` tool — ``app/services/tool_handlers/delegate_tool.py``
* ``ask_data_agent`` tool — ``app/services/tool_handlers/delegation_tools.py``

Both run their own LLM+tool-call loops that previously bypassed the
reliability stack. This module provides service-layer wrappers so the
sub-agent paths get: prompt caching (P3), structured error classification
(P1), message sanitization + pre-API pruning (P1.3/P2), per-result +
per-turn persistence (P0 Layer 2/3), guardrail controller (P0), iteration
budget (P0), and metrics (P4) — without importing the router-level
``_call_llm_with_tools`` (which would create a circular import and raise
``HTTPException``, inappropriate inside a tool handler).
"""
from __future__ import annotations

import json
import logging
import os

import httpx

from app.config import settings
from app.services.llm_router import LLMEndpoint
from app.services.llm_service import (
    llm_headers, llm_url, get_model, model_has_fixed_temperature,
    _supports_parallel_tool_calls,
)
from app.services.prompt_caching import apply_cache_control
from app.services.api_error_classifier import classify_api_error
from app.services.agent_metrics import metrics
from app.services.message_sanitization import sanitize_messages
from app.services.compaction.pre_api_prune import prune_tool_results_only
from app.services.tool_result_persistence import (
    persist_tool_result,
    budget_for_context_window,
)
from app.services.tool_loop_guardrails import (
    ToolLoopGuardController,
    synthetic_blocked_result,
)
from app.services.iteration_budget import IterationBudget

logger = logging.getLogger(__name__)

__all__ = [
    "call_llm_with_reliability",
    "pre_call_prep",
    "persist_result_str",
    "apply_turn_budget_to_batch",
    "ToolLoopGuardController",
    "synthetic_blocked_result",
    "IterationBudget",
    "metrics",
]


# ---------------------------------------------------------------------------
# LLM call with reliability (P1 error classification + P3 prompt caching)
# ---------------------------------------------------------------------------

async def call_llm_with_reliability(
    messages: list[dict],
    tools: list[dict] | None,
    *,
    temperature: float = 0.7,
    endpoint: LLMEndpoint | None = None,
) -> dict:
    """Call the LLM with prompt caching and structured error classification.

    Replaces the raw ``_call_sub_llm`` / ``_call_llm`` helpers in the
    sub-agent paths. Unlike the router-level ``_call_llm_with_tools``, this
    does **not** raise ``HTTPException`` — it re-raises the original
    exception so the sub-agent loop can handle it gracefully.

    Args:
        messages: Conversation messages (cache-control applied to a copy).
        tools: OpenAI-format tool schemas, or None/empty for no tools.
        temperature: Sampling temperature (sub-agents default 0.7; the Data
            Agent uses 0.2).
        endpoint: Optional concrete ``LLMEndpoint`` from hierarchical config.
            When set, uses endpoint.base_url / endpoint.api_key /
            endpoint.model_id instead of global settings. When None, falls
            back to the legacy global provider.

    Returns:
        dict with ``content`` (str), ``tool_calls`` (list), ``reasoning`` (str).
    """
    if endpoint is not None:
        _model = endpoint.model_id
        _url = endpoint.base_url.rstrip("/") + "/chat/completions"
        _headers = {"Authorization": f"Bearer {endpoint.api_key}", "Content-Type": "application/json"}
    else:
        _model = get_model()
        _url = llm_url()
        _headers = llm_headers()

    payload = {
        "model": _model,
        "messages": apply_cache_control(
            messages,
            enabled=getattr(settings, "PROMPT_CACHE_ENABLED", False),
            cache_ttl=getattr(settings, "PROMPT_CACHE_TTL", "5m"),
        ),
    }
    if not model_has_fixed_temperature(_model):
        payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools
    # P0-2: parallel tool calls capability injection (default off; gated
    # on the helper so anthropic models never receive the field).
    if (
        getattr(settings, "LLM_PARALLEL_TOOL_CALLS_ENABLED", False)
        and _supports_parallel_tool_calls(_model)
    ):
        payload["parallel_tool_calls"] = True

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(_url, headers=_headers, json=payload)
            _sc = getattr(resp, "status_code", 0)
            if isinstance(_sc, int) and _sc >= 400:
                logger.warning(
                    "Sub-agent LLM HTTP %s body: %s",
                    _sc, resp.text[:500],
                )
            resp.raise_for_status()
    except Exception as e:
        # P1: structured error classification + metrics
        ce = classify_api_error(e)
        logger.warning(
            "Sub-agent LLM call failed: reason=%s retryable=%s "
            "should_compress=%s should_fallback=%s",
            ce.reason.value, ce.retryable, ce.should_compress, ce.should_fallback,
        )
        metrics.record_error(ce.reason.value)
        raise

    data = resp.json()
    choice = data["choices"][0]
    message = choice.get("message", {})
    return {
        "content": message.get("content", "") or "",
        "tool_calls": message.get("tool_calls", []) or [],
        "reasoning": message.get("reasoning_content") or "",
    }


# ---------------------------------------------------------------------------
# Pre-API-call prep (P1.3 pruning + P2 sanitization + orphan-tool-pair fix)
# ---------------------------------------------------------------------------

def pre_call_prep(messages: list[dict]) -> None:
    """Prune old tool results, sanitize messages, AND drop orphan
    ``assistant.tool_calls`` before an LLM API call.

    Sequence:
      1. ``prune_tool_results_only(messages)`` — shrink oversized tool outputs.
      2. ``sanitize_messages(messages)`` — surrogates / closed interrupted
         tails / mid-list system demotion.
      3. ``_sanitize_tool_call_pairing(messages)`` — defense-in-depth against
         mid-conversation assistant-with-tool_calls messages that lost their
         matching tool_result (truncate-by-mistake, message-rebuild race, or
         partial persistence). Without this guard, deepseek/QWen/vLLM APIs
         return HTTP 400 — "An assistant message with 'tool_calls' must be
         followed by tool messages responding to each 'tool_call_id'."
         The sub-agent path (``call_llm_with_reliability``) does NOT pass
         through ``llm_service._sanitize_tool_call_pairing``, so we must
         invoke it here. See ``llm_service.sanitize_tool_call_pairing`` for
         full contract. Mutates ``messages`` in place.

    Mirrors the sequence used by the main agent loop
    (``agents.py`` lines ~2397-2398 + ``llm_service``).
    """
    prune_tool_results_only(messages)
    sanitize_messages(messages)
    # Defense-in-depth: strip orphan assistant.tool_calls (idempotent if
    # already clean). Uses the canonical sanitizer from llm_service so the
    # sub-agent and main-loop rules stay in sync.
    try:
        from app.services.llm_service import _sanitize_tool_call_pairing
        cleaned = _sanitize_tool_call_pairing(messages)
        if len(cleaned) != len(messages) or any(
            m1.get("tool_calls") != m2.get("tool_calls")
            for m1, m2 in zip(cleaned, messages)
        ):
            logger.warning(
                "pre_call_prep: orphan assistant.tool_calls sanitized (%d → %d)",
                len(messages), len(cleaned),
            )
        messages[:] = cleaned
    except Exception as exc:
        # Sanitizer must never break the caller; fall through with a
        # warning and hope the main sanitize pass caught it.
        logger.warning("pre_call_prep: _sanitize_tool_call_pairing skipped: %s", exc)


# ---------------------------------------------------------------------------
# Tool result persistence (P0 Layer 2 per-result + Layer 3 per-turn)
# ---------------------------------------------------------------------------

def persist_result_str(
    tool_name: str, result: dict, conversation_id: str | None,
    *,
    context_window_tokens: int | None = None,
) -> str:
    """Serialize a tool result dict, applying Layer 2 per-result persistence.

    Large results are written to disk and replaced with an inline preview +
    pointer. Returns the JSON string to append to the sub-agent's message
    list. Mirrors ``agents.py:_persisted_result_str``.
    """
    result_str = json.dumps(result, ensure_ascii=False, default=str)
    try:
        from app.services.compaction import get_context_window
        storage_dir = os.path.join(
            getattr(settings, "AGENT_WORKSPACE_DIR", "agent_workspace"),
            settings.TOOL_RESULT_STORAGE_DIR,
        )
        # Real per-model window (admin-set or auto-probed) drives budgets —
        # never the name heuristic alone, so ANY model works.
        ctx_window = get_context_window(
            get_model(), context_window_tokens=context_window_tokens,
        )
        config = budget_for_context_window(ctx_window)
        new_str, _meta = persist_tool_result(
            tool_name, result_str, storage_dir, config, conversation_id
        )
        return new_str
    except Exception as e:
        logger.debug("Sub-agent tool result persistence failed (non-fatal): %s", e)
        return result_str


def apply_turn_budget_to_batch(
    messages: list[dict],
    batch_tool_call_ids: list[str],
    batch_tool_names: list[str],
    conversation_id: str | None,
    *,
    context_window_tokens: int | None = None,
) -> None:
    """Layer 3: if total tool output in this batch exceeds budget, spill largest.

    Scans the ``role:"tool"`` messages appended in this iteration (identified
    by ``batch_tool_call_ids``) and replaces the largest ones with
    disk-persisted previews if the aggregate size exceeds the turn budget.
    Mirrors ``agents.py:_apply_turn_budget_to_messages``.
    """
    try:
        from app.services.compaction import get_context_window
        batch_contents: list[tuple[str, str, str]] = []
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            tid = msg.get("tool_call_id")
            if tid not in batch_tool_call_ids:
                continue
            idx = batch_tool_call_ids.index(tid)
            batch_contents.append(
                (tid, batch_tool_names[idx], msg.get("content", ""))
            )
        if not batch_contents:
            return

        total = sum(len(c) for _, _, c in batch_contents)
        config = budget_for_context_window(
            get_context_window(
                get_model(), context_window_tokens=context_window_tokens,
            )
        )
        if total <= config.turn_budget_chars:
            return

        storage_dir = os.path.join(
            getattr(settings, "AGENT_WORKSPACE_DIR", "agent_workspace"),
            settings.TOOL_RESULT_STORAGE_DIR,
        )
        sorted_contents = sorted(
            batch_contents, key=lambda x: len(x[2]), reverse=True
        )
        current_total = total
        new_contents: dict[str, str] = {}
        for tid, tool_name, content in sorted_contents:
            if current_total <= config.turn_budget_chars:
                new_contents[tid] = content
                continue
            if tool_name in config.no_persist_tools:
                new_contents[tid] = content
                continue
            new_str, meta = persist_tool_result(
                tool_name, content, storage_dir, config,
                conversation_id, force=True,
            )
            if meta["persisted"]:
                current_total -= len(content) - len(new_str)
                new_contents[tid] = new_str
            else:
                new_contents[tid] = content

        for msg in messages:
            if msg.get("role") != "tool":
                continue
            tid = msg.get("tool_call_id")
            if tid in new_contents:
                msg["content"] = new_contents[tid]
    except Exception as e:
        logger.debug("Sub-agent turn budget application failed (non-fatal): %s", e)
