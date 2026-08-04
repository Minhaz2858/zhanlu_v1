"""RAG tool handlers — tools for the rag_research_agent.

These tools allow agents and the orchestrator to query the hybrid RAG
knowledge base for industry reports, weekly reports, market signals,
past decisions, causal chains, news events, products, and user memory.

Tools registered:
    rag_hybrid_search           — Search a domain collection with hybrid (dense+lexical) RRF.
    rag_search_industry_reports — Convenience: search industry_reports collection.
    rag_search_weekly_reports   — Convenience: search weekly_reports collection.
    rag_search_past_decisions   — Search past_decisions collection.
    rag_search_market_signals   — Search market_signals collection.
    rag_search_news_events      — Search news_events collection.
    rag_search_user_memory      — Search user_memory collection.
    rag_search_products         — Search product_catalog collection.
    rag_build_context           — Build a multi-collection context block.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def _string_schema(description: str) -> dict:
    return {"type": "string", "description": description}


_HYBRID_SEARCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "query": _string_schema("Natural-language query."),
        "collection": _string_schema(
            "One of the 9 semantic collections: industry_reports, weekly_reports, "
            "past_decisions, market_signals, causal_graph_embeddings, news_events, "
            "decision_outcomes, product_catalog, user_memory."
        ),
        "top_k": {"type": "integer", "description": "Number of hits (1-20).", "default": 5},
        "user_id": _string_schema("Optional user id for per-user filtering."),
    },
    "required": ["query", "collection"],
}

_COLLECTION_SEARCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "query": _string_schema("Natural-language query."),
        "top_k": {"type": "integer", "description": "Number of hits (1-20).", "default": 5},
        "user_id": _string_schema("Optional user id for per-user filtering."),
    },
    "required": ["query"],
}

_BUILD_CONTEXT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "query": _string_schema("Natural-language query."),
        "collections": {
            "type": "array",
            "items": _string_schema("Collection name"),
            "description": "List of collections to query. Default: 5 main collections.",
        },
        "max_chars": {"type": "integer", "description": "Max chars in returned block.", "default": 1800},
    },
    "required": ["query"],
}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _rag_hybrid_search(args: dict, db: Any, user_id: str, context: dict = None) -> dict:
    """Search a domain collection with hybrid (dense+lexical) RRF."""
    query = (args.get("query") or "").strip()
    collection = (args.get("collection") or "industry_reports").strip()
    top_k = int(args.get("top_k", 5))
    if not query:
        return {"success": False, "error": "query is required"}
    try:
        from app.services.rag.rag_retriever import retrieve_rich
        chunks = retrieve_rich(query=query, n_results=top_k, collection=collection)
        return {
            "success": True,
            "collection": collection,
            "hits": [
                {
                    "text": c.text,
                    "score": c.score,
                    "source": c.source,
                    "file_stem": c.file_stem,
                    "chunk_index": c.chunk_index,
                    "total_chunks": c.total_chunks,
                }
                for c in chunks
            ],
            "count": len(chunks),
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("rag_hybrid_search failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def _rag_collection_search(
    args: dict,
    db: Any,
    user_id: str,
    context: dict = None,
    *,
    collection: str,
) -> dict:
    """Convenience handler that calls retrieve_rich on a fixed collection."""
    args2 = dict(args)
    args2["collection"] = collection
    return await _rag_hybrid_search(args2, db, user_id, context)


async def _build_context(args: dict, db: Any, user_id: str, context: dict = None) -> dict:
    """Build a multi-collection context block."""
    query = (args.get("query") or "").strip()
    collections = args.get("collections")
    max_chars = int(args.get("max_chars", 1800))
    if not query:
        return {"success": False, "error": "query is required"}
    try:
        from app.services.rag.domain_collections import build_domain_context
        block = build_domain_context(query, collections=collections, max_chars=max_chars)
        return {"success": True, "context": block, "length": len(block)}
    except Exception as exc:  # noqa: BLE001
        logger.error("rag_build_context failed: %s", exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


_REGISTRATIONS: tuple = (
    (
        "rag_hybrid_search",
        _HYBRID_SEARCH_SCHEMA,
        _rag_hybrid_search,
        "Hybrid (dense+lexical) RRF search of any of the 9 semantic collections.",
    ),
    (
        "rag_search_industry_reports",
        _COLLECTION_SEARCH_SCHEMA,
        lambda a, db, uid, ctx=None: _rag_collection_search(a, db, uid, ctx, collection="industry_reports"),
        "Search the industry_reports collection.",
    ),
    (
        "rag_search_weekly_reports",
        _COLLECTION_SEARCH_SCHEMA,
        lambda a, db, uid, ctx=None: _rag_collection_search(a, db, uid, ctx, collection="weekly_reports"),
        "Search the weekly_reports collection.",
    ),
    (
        "rag_search_past_decisions",
        _COLLECTION_SEARCH_SCHEMA,
        lambda a, db, uid, ctx=None: _rag_collection_search(a, db, uid, ctx, collection="past_decisions"),
        "Search the past_decisions collection.",
    ),
    (
        "rag_search_market_signals",
        _COLLECTION_SEARCH_SCHEMA,
        lambda a, db, uid, ctx=None: _rag_collection_search(a, db, uid, ctx, collection="market_signals"),
        "Search the market_signals collection.",
    ),
    (
        "rag_search_news_events",
        _COLLECTION_SEARCH_SCHEMA,
        lambda a, db, uid, ctx=None: _rag_collection_search(a, db, uid, ctx, collection="news_events"),
        "Search the news_events collection.",
    ),
    (
        "rag_search_user_memory",
        _COLLECTION_SEARCH_SCHEMA,
        lambda a, db, uid, ctx=None: _rag_collection_search(a, db, uid, ctx, collection="user_memory"),
        "Search the user_memory collection.",
    ),
    (
        "rag_search_products",
        _COLLECTION_SEARCH_SCHEMA,
        lambda a, db, uid, ctx=None: _rag_collection_search(a, db, uid, ctx, collection="product_catalog"),
        "Search the product_catalog collection.",
    ),
    (
        "rag_build_context",
        _BUILD_CONTEXT_SCHEMA,
        _build_context,
        "Build a multi-collection context block for prompt injection.",
    ),
)


for _name, _schema, _handler, _desc in _REGISTRATIONS:
    registry.register(
        name=_name,
        schema=_schema,
        handler=_handler,
        category="rag",
        enabled_by_default=False,
        description=_desc,
    )