"""Knowledge graph tool handler for the Zhanlu agent runtime.

Provides `ask_knowledge_graph` — query the product knowledge graph
(the catalog is defined by the app's domain configuration).
Registered via `register_knowledge_graph_tools(registry)` only when
`KNOWLEDGE_GRAPH_ENABLED` is True.

Uses the config-driven graph + resolver; no DB dependency.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

from .graph import (
    get_domain_graph,
    query_upstream,
    query_downstream,
    query_chain,
    format_graph_summary,
)
from .resolver import (
    resolve_product_id,
    extract_product_ids_in_text,
    split_product_tokens,
    product_id_to_context_label,
    list_supported_product_ids,
)


async def _handle_knowledge_graph(
    args: dict,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Handle `ask_knowledge_graph` calls.

    Args:
        question: Natural-language query (e.g. "what are the upstream products of a product?")
        product: Optional product name/ID to scope the query
        mode: "upstream", "downstream", "chain", "summary", or "resolve" (default: auto-detect)

    Returns:
        {"success": True, "data": {...}} or {"success": False, "error": "..."}
    """
    question: str = (args.get("question") or "").strip()
    product: str = (args.get("product") or "").strip()
    mode: str = (args.get("mode") or "auto").strip().lower()

    # ── Resolve product ───────────────────────────────────────────────
    product_id: str | None = None
    if product:
        product_id = resolve_product_id(product)
        if not product_id:
            # Try extracting from the question
            if question:
                product_id = resolve_product_id(question)
            if not product_id:
                return {
                    "success": True,
                    "data": {
                        "message": f"Could not resolve product '{product}'. "
                        f"Supported products: {', '.join(list_supported_product_ids())}",
                        "supported_products": list_supported_product_ids(),
                    },
                }
    elif question:
        product_id = resolve_product_id(question)

    # ── Auto-detect mode from question ────────────────────────────────
    if mode == "auto" and question:
        q_lower = question.lower()
        if any(w in q_lower for w in ["上游", "upstream", "feed into", "feedstock", "原料"]):
            mode = "upstream"
        elif any(w in q_lower for w in ["下游", "downstream", "produce", "derive", "下游产品"]):
            mode = "downstream"
        elif any(w in q_lower for w in ["链", "chain", "path", "路径", "流程"]):
            mode = "chain"
        elif any(w in q_lower for w in ["概要", "summary", "overview", "概览", "总览", "全部"]):
            mode = "summary"
        elif any(w in q_lower for w in ["替代", "substitute", "替代品", "竞争"]):
            mode = "chain"
        elif product_id:
            # If we have a product but no clear mode, show both upstream + downstream
            mode = "full"
        else:
            mode = "summary"

    # ── Execute ───────────────────────────────────────────────────────
    try:
        if mode == "upstream" and product_id:
            result = query_upstream(product_id)
            return {"success": True, "data": result}

        elif mode == "downstream" and product_id:
            result = query_downstream(product_id)
            return {"success": True, "data": result}

        elif mode == "chain" and product_id:
            # Try both directions and return the richer one
            up = query_chain(product_id, "upstream")
            down = query_chain(product_id, "downstream")
            direction = "downstream" if len(down.get("downstream_chains", [])) >= len(up.get("upstream_chains", [])) else "upstream"
            return {"success": True, "data": query_chain(product_id, direction)}

        elif mode == "full" and product_id:
            upstream = query_upstream(product_id)
            downstream = query_downstream(product_id)
            chain = query_chain(product_id, "downstream")
            return {
                "success": True,
                "data": {
                    "product_id": product_id,
                    "upstream": upstream.get("upstream", []),
                    "downstream": downstream.get("downstream", []),
                    "chains": chain.get("downstream_chains", []),
                    "substitutes": chain.get("substitutes", []),
                },
            }

        elif mode == "resolve" and question:
            ids = extract_product_ids_in_text(question)
            if not ids:
                ids = split_product_tokens(question)
            return {
                "success": True,
                "data": {
                    "resolved_product_ids": ids,
                    "labels": {pid: product_id_to_context_label(pid) for pid in ids},
                },
            }

        else:
            # Summary / overview
            summary = format_graph_summary()
            return {
                "success": True,
                "data": {
                    "summary": summary,
                    "product_count": len(list_supported_product_ids()),
                    "supported_products": list_supported_product_ids(),
                    "hint": "Use mode=upstream/downstream/chain with a specific product_id for details.",
                },
            }

    except Exception as e:
        logger.exception("Knowledge graph query failed: %s", e)
        return {"success": False, "error": str(e)}


async def _handle_resolve_products(
    args: dict,
    db=None,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Resolve product names in a query string to canonical product IDs.

    Lightweight companion to ask_knowledge_graph — returns just the product IDs.
    """
    text: str = (args.get("text") or args.get("question") or "").strip()
    if not text:
        return {"success": False, "error": "text or question is required"}

    ids = extract_product_ids_in_text(text)
    if not ids:
        ids = split_product_tokens(text)

    return {
        "success": True,
        "data": {
            "input": text,
            "resolved_product_ids": ids,
            "labels": {pid: product_id_to_context_label(pid) for pid in ids},
        },
    }


# ── Registration ──────────────────────────────────────────────────────────


def register_knowledge_graph_tools(registry) -> None:
    """Register knowledge graph tools in the tool registry.

    Safe to call multiple times (idempotent).  Only registers if
    KNOWLEDGE_GRAPH_ENABLED is True (checked at call site).
    """
    try:
        registry.register(
            name="ask_knowledge_graph",
            handler=_handle_knowledge_graph,
            schema={
                "type": "function",
                "function": {
                    "name": "ask_knowledge_graph",
                    "description": (
                        "Query the product knowledge graph for product "
                        "relationships; the catalog is defined by the app's "
                        "domain configuration. Use mode='upstream' to find what "
                        "feeds into a product, 'downstream' for what it produces, "
                        "'chain' for the full value chain, 'summary' for an "
                        "overview of the configured catalog."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "Natural language query about product relationships, e.g. 'what are the upstream products of a product?'",
                            },
                            "product": {
                                "type": "string",
                                "description": "Canonical product ID or Chinese/English name (list supported products via a summary query).",
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["auto", "upstream", "downstream", "chain", "summary", "resolve", "full"],
                                "description": "Query mode: auto-detect, upstream, downstream, chain path, summary overview, resolve product names, or full (both directions).",
                            },
                        },
                        "required": ["question"],
                    },
                },
            },
            description="Query the product knowledge graph for upstream/downstream/chains.",
        )
        registry.register(
            name="resolve_products",
            handler=_handle_resolve_products,
            schema={
                "type": "function",
                "function": {
                    "name": "resolve_products",
                    "description": (
                        "Resolve free-text product names (Chinese, English, aliases) "
                        "to canonical product IDs (catalog from the app's domain "
                        "configuration). Use this to extract which products a user "
                        "is asking about."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "Free-text query that may contain product names.",
                            },
                        },
                        "required": ["text"],
                    },
                },
            },
            description="Resolve product names to canonical IDs.",
        )
        logger.info("Knowledge graph tools registered (ask_knowledge_graph, resolve_products)")
    except Exception as e:
        logger.warning("Knowledge graph tool registration skipped: %s", e)
