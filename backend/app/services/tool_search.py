"""Semantic tool-selection — selective schema injection (IBM tool-calling, P0-2).

Problem: the eligible tool catalog (65 handler modules + CRUD tools) was
injected wholesale into every LLM call, so the model waded through dozens
of irrelevant schemas — token cost and selection noise growing with the
catalog (the IBM tool-calling article's "tool selection mechanism" gap).

Mechanism:
  - Rank eligible tool schemas by semantic similarity to the user message
    (batch embeddings via ``memory_advanced.embeddings`` with caching;
    lexical token-overlap fallback when no embedding backend is set).
  - Inject only the top-K most relevant schemas plus a small ALWAYS-ON
    essential set (grounding + delegation), and append the ``search_tools``
    meta-tool so the model can discover + call anything filtered out —
    tool execution resolves handlers by name from the ToolRegistry at
    call time, so discovered tools are immediately callable.

Fail-safe: any error returns the original unfiltered list.
"""
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 32

# Tools that must never be filtered out (grounding, delegation, discovery).
ALWAYS_KEEP = {
    "web_search",
    "web_extract",
    "delegate_task",
    "ask_data_agent",
    "search_tools",
}

SEARCH_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_tools",
        "description": (
            "Search the full tool catalog by keyword when the tool you need "
            "is not in your current tool list. Returns matching tool names, "
            "descriptions and parameter schemas — call the returned tool "
            "name directly afterwards."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What the tool should do, e.g. 'generate an image'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matches to return (default 5).",
                },
            },
            "required": ["query"],
        },
    },
}


def _tool_text(schema: dict) -> str:
    fn = (schema or {}).get("function", {})
    params = fn.get("parameters", {}).get("properties", {}) or {}
    param_names = " ".join(str(p) for p in params.keys())
    return " ".join([
        str(fn.get("name", "")),
        str(fn.get("description", "")),
        param_names,
    ]).strip()


def _lexical_scores(query: str, texts: list[str]) -> list[float]:
    """Token-overlap fallback when embeddings are unavailable."""
    q_tokens = {t for t in query.lower().split() if len(t) > 2}
    out: list[float] = []
    for text in texts:
        t_tokens = {t for t in text.lower().split() if len(t) > 2}
        if not q_tokens or not t_tokens:
            out.append(0.0)
            continue
        overlap = len(q_tokens & t_tokens)
        out.append(overlap / (len(q_tokens) ** 0.5 * len(t_tokens) ** 0.5))
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def rank_tools(query: str, schemas: list[dict]) -> list[float]:
    """Relevance score per schema (higher = more relevant). Never raises.

    Uses batch embeddings when a backend is configured, else lexical
    token overlap.
    """
    texts = [_tool_text(s) for s in schemas]
    if not query or not query.strip() or not schemas:
        return [0.0] * len(schemas)
    try:
        from app.services.memory_advanced.embeddings import (
            get_embedding,
            get_embeddings_batch,
        )

        q_vec = get_embedding(query)
        if q_vec and q_vec.vector:
            vecs = get_embeddings_batch(texts)
            if any(vecs):
                return [
                    _cosine(q_vec.vector, v) if v else 0.0 for v in vecs
                ]
    except Exception as exc:  # noqa: BLE001 — ranking must never break the loop
        logger.debug("tool_search: embedding ranking failed (%s)", exc)
    return _lexical_scores(query, texts)


def select_relevant_tools(
    user_message: str | None,
    schemas: list[dict],
    *,
    top_k: int | None = None,
) -> list[dict]:
    """Filter tool schemas to the top-K relevant for this message.

    Returns the input unchanged when the flag is off, the message is
    empty, or the catalog already fits within the budget. Otherwise keeps
    the top-K ranked schemas + ALWAYS_KEEP essentials and appends the
    ``search_tools`` meta-tool. Never raises.
    """
    try:
        from app.config import settings

        if not getattr(settings, "TOOL_SEARCH_ENABLED", False):
            return schemas
        k = int(top_k or getattr(settings, "TOOL_SEARCH_TOP_K", DEFAULT_TOP_K))
        if not user_message or not user_message.strip() or len(schemas) <= k:
            return schemas

        names = [
            (s.get("function", {}) or {}).get("name", "") for s in schemas
        ]
        scores = rank_tools(user_message, schemas)
        order = sorted(range(len(schemas)), key=lambda i: -scores[i])
        keep = set(ALWAYS_KEEP)
        selected: list[dict] = []
        for i in order:
            if len(selected) >= k:
                break
            if names[i] in keep:
                continue  # added at the end, order-preserving below
            selected.append(schemas[i])
        # essentials first (stable, original order), then the ranked rest
        essentials = [s for s in schemas if (s.get("function", {}) or {}).get("name") in keep]
        merged = essentials + [
            s for s in selected if s not in essentials
        ]
        if not any((s.get("function", {}) or {}).get("name") == "search_tools" for s in merged):
            merged.append(dict(SEARCH_TOOLS_SCHEMA))
        logger.info(
            "tool_search: injected %d/%d schemas for message %r",
            len(merged), len(schemas), (user_message or "")[:60],
        )
        return merged
    except Exception as exc:  # noqa: BLE001 — never break tool assembly
        logger.warning("tool_search: selection failed, keeping all tools (%s)", exc)
        return schemas


def search_catalog(query: str, limit: int = 5) -> list[dict]:
    """Rank the FULL registered catalog (RBAC-filtered) against ``query``.

    Backs the ``search_tools`` meta-tool: returns compact schema cards
    (name / description / parameters) for the best matches.
    """
    from app.services.tool_registry import registry

    try:
        names = list(registry.list_available())
    except Exception:  # noqa: BLE001
        names = []
    schemas = registry.get_schemas(names)
    scores = rank_tools(query, schemas)
    order = sorted(range(len(schemas)), key=lambda i: -scores[i])[: max(1, limit)]
    out = []
    for i in order:
        fn = (schemas[i] or {}).get("function", {})
        if not fn.get("name"):
            continue
        out.append({
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        })
    return out


def handle_search_tools(
    args: dict,
    db=None,
    user_id=None,
    context: dict | None = None,
) -> dict:
    """Registry handler for the ``search_tools`` meta-tool.

    Signature matches the governed invocation contract
    (``(args, db, user_id, context)``) — the registry dispatches sync
    handlers with all three positional args, and a guessed-name invocation
    via ``execute_tool`` arrives here the same way. Discovery honesty
    (milestone acceptance, 2026-11-01): results are filtered by the
    CALLER's effective tool policy, so ``search_tools`` never advertises a
    tool the agent is forbidden to invoke.
    """
    query = str((args or {}).get("query", "")).strip()
    limit = int((args or {}).get("limit", 5) or 5)
    if not query:
        return {"success": False, "error": "query is required"}
    matches = search_catalog(query, limit=max(1, min(limit, 20)))
    # Caller policy filter — never advertise forbidden tools. Extra cards
    # beyond the limit may be dropped by the filter; over-fetch so the
    # caller still gets a full page of ALLOWED matches.
    try:
        from app.services.agent_tool_policy import (
            load_tool_policy,
            policy_filters_catalog,
        )

        _policy = load_tool_policy(db, (context or {}).get("agent_app_id"))
        if _policy:
            _over = search_catalog(query, limit=max(1, min(limit, 20)) * 3)
            _filtered = policy_filters_catalog(_policy, _over)
            matches = _filtered[: max(1, min(limit, 20))]
    except Exception:  # noqa: BLE001 — discovery must never break execution
        pass
    if not matches:
        return {
            "success": True,
            "matches": [],
            "note": "no matching tools — describe what you need in the answer instead",
        }
    return {
        "success": True,
        "matches": matches,
        "note": "call the returned tool name directly with these parameters",
    }


__all__ = [
    "rank_tools",
    "select_relevant_tools",
    "search_catalog",
    "handle_search_tools",
    "SEARCH_TOOLS_SCHEMA",
    "ALWAYS_KEEP",
]
