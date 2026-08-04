"""Domain collection services — write/read helpers for the 9 semantic collections.

Consolidated module: 5 thin service classes plus a registry and a context-builder
helper. Each service operates on a specific ChromaDB semantic collection.

Services (with their collection name):
    DecisionService  → past_decisions + decision_outcomes
    SignalService    → market_signals
    CausalService    → causal_graph_embeddings
    NewsService      → news_events  (Phase 1 bridge)
    ProductService   → product_catalog

Registry:
    get_service(name)  → service instance or None
    list_all_services() → dict[name → service]

Context builder:
    build_domain_context(query, collections=[...]) → formatted prompt string
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from app.services.rag.collection_names import (
    CAUSAL_GRAPH_EMBEDDINGS,
    DECISION_OUTCOMES,
    MARKET_SIGNALS,
    NEWS_EVENTS,
    PAST_DECISIONS,
    PRODUCT_CATALOG,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_kb() -> Any:
    try:
        from app.services.rag.knowledge_base import create_knowledge_base
    except Exception as exc:  # noqa: BLE001
        logger.debug("_get_kb: import failed: %s", exc)
        return None
    try:
        org_id = os.environ.get("DEFAULT_ORG_ID", "default")
        return create_knowledge_base(org_id=org_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("_get_kb: create_knowledge_base failed: %s", exc)
        return None


def _safe_meta(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    meta = dict(metadata) if metadata else {}
    if not meta:
        meta = {"_indexed_at": "auto"}
    return meta


def _format_hits(hits: List[Dict[str, Any]]) -> str:
    """Format a list of hit dicts as a prompt-injection block."""
    if not hits:
        return ""
    lines = []
    for hit in hits:
        text = (hit.get("text", "") or "").replace("\n", " ").strip()[:300]
        if len(text) >= 300:
            text += "…"
        meta = hit.get("metadata", {}) or {}
        topic = meta.get("topic") or meta.get("headline") or meta.get("name") or ""
        prefix = f"[{topic}] " if topic else ""
        lines.append(f"{prefix}{text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Base service
# ---------------------------------------------------------------------------


class _BaseService:
    collection_name: str = ""

    def _kb(self) -> Any:
        return _get_kb()

    def _coll(self) -> Any:
        kb = self._kb()
        if kb is None or not self.collection_name:
            return None
        if hasattr(kb, "get_collection"):
            try:
                return kb.get_collection(self.collection_name)
            except Exception:  # noqa: BLE001
                return None
        return None

    def upsert(
        self,
        item_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        coll = self._coll()
        if coll is None or not text:
            return False
        try:
            meta = _safe_meta(metadata)
            # If the KB exposes a typed upsert (knowledge_base interface), use it
            kb = self._kb()
            if hasattr(kb, "upsert"):
                kb.upsert(
                    name=self.collection_name,
                    doc_id=item_id,
                    text=text,
                    metadata=meta,
                )
                return True
            # Fallback to raw coll.upsert via _embed_one
            if hasattr(kb, "_get_or_create"):
                real_coll = kb._get_or_create(self.collection_name)
                vec = kb._embed_one(text)
                real_coll.upsert(ids=[item_id], embeddings=[vec],
                                 documents=[text], metadatas=[meta])
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s.upsert failed: %s", type(self).__name__, exc)
        return False

    def query(self, query_text: str, top_k: int = 5) -> List[Dict[str, Any]]:
        coll = self._coll()
        if coll is None or not query_text:
            return []
        try:
            res = coll.query(query_texts=[query_text], n_results=top_k,
                              include=["documents", "metadatas", "distances"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s.query failed: %s", type(self).__name__, exc)
            return []
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        hits: List[Dict[str, Any]] = []
        import math
        for doc, meta, dist in zip(docs, metas, dists):
            if not doc:
                continue
            try:
                score = math.exp(-float(dist)) if dist is not None else 0.0
            except (TypeError, ValueError):
                score = 0.0
            hits.append({
                "text": doc,
                "metadata": meta or {},
                "score": score,
            })
        return hits


# ---------------------------------------------------------------------------
# DecisionService — past_decisions + decision_outcomes
# ---------------------------------------------------------------------------


class DecisionService(_BaseService):
    """Manages past_decisions (the decision itself) and decision_outcomes
    (the result of executing the decision)."""

    past_decisions_collection: str = PAST_DECISIONS
    outcomes_collection: str = DECISION_OUTCOMES

    # Switch which collection ``upsert``/``query`` operate on
    @property
    def _active_collection(self) -> str:
        return getattr(self, "_mode", self.past_decisions_collection)

    @property
    def collection_name(self) -> str:  # type: ignore[override]
        return self._active_collection

    def upsert_decision(self, decision_id: str, summary: str,
                        metadata: Optional[Dict[str, Any]] = None) -> bool:
        self._mode = self.past_decisions_collection
        meta = dict(metadata or {})
        meta["decision_id"] = decision_id
        return self.upsert(decision_id, summary, meta)

    def upsert_outcome(self, outcome_id: str, outcome: str,
                       metadata: Optional[Dict[str, Any]] = None) -> bool:
        self._mode = self.outcomes_collection
        meta = dict(metadata or {})
        meta["outcome_id"] = outcome_id
        return self.upsert(outcome_id, outcome, meta)

    def query_decisions(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        self._mode = self.past_decisions_collection
        return self.query(query, top_k)

    def query_outcomes(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        self._mode = self.outcomes_collection
        return self.query(query, top_k)


# ---------------------------------------------------------------------------
# SignalService — market_signals
# ---------------------------------------------------------------------------


class SignalService(_BaseService):
    collection_name = MARKET_SIGNALS


# ---------------------------------------------------------------------------
# CausalService — causal_graph_embeddings
# ---------------------------------------------------------------------------


class CausalService(_BaseService):
    collection_name = CAUSAL_GRAPH_EMBEDDINGS

    def upsert_chain(self, chain_id: str, description: str,
                     nodes: Optional[List[str]] = None,
                     edges: Optional[List[tuple]] = None) -> bool:
        meta = {
            "chain_id": chain_id,
            "nodes": nodes or [],
            "edges": [list(e) for e in (edges or [])],
        }
        return self.upsert(chain_id, description, meta)


# ---------------------------------------------------------------------------
# NewsService — news_events (Phase 1 bridge)
# ---------------------------------------------------------------------------


class NewsService(_BaseService):
    collection_name = NEWS_EVENTS

    def upsert_event(self, event_id: str, headline: str,
                     key_information: str = "",
                     metadata: Optional[Dict[str, Any]] = None) -> bool:
        body = headline
        if key_information:
            body = f"{headline} — {key_information}"
        meta = dict(metadata or {})
        meta["event_id"] = event_id
        meta["headline"] = headline
        if key_information:
            meta["key_information"] = key_information
        return self.upsert(event_id, body, meta)


# ---------------------------------------------------------------------------
# ProductService — product_catalog
# ---------------------------------------------------------------------------


class ProductService(_BaseService):
    collection_name = PRODUCT_CATALOG

    def upsert_product(self, product_id: str, name: str,
                       aliases: Optional[List[str]] = None,
                       category: str = "") -> bool:
        alias_text = " | ".join(aliases or [])
        body = f"{name} ({category}) — aliases: {alias_text}" if alias_text else f"{name} ({category})"
        meta = {
            "product_id": product_id,
            "name": name,
            "aliases": aliases or [],
            "category": category,
        }
        return self.upsert(product_id, body, meta)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_SERVICES: Dict[str, _BaseService] = {
    "decision": DecisionService(),
    "signal": SignalService(),
    "causal": CausalService(),
    "news": NewsService(),
    "product": ProductService(),
}


def get_service(name: str) -> Optional[_BaseService]:
    return _SERVICES.get(name)


def list_all_services() -> Dict[str, _BaseService]:
    return dict(_SERVICES)


# ---------------------------------------------------------------------------
# build_domain_context — multi-collection context block for prompts
# ---------------------------------------------------------------------------


def build_domain_context(
    query: str,
    collections: Optional[List[str]] = None,
    max_chars: int = 1800,
    top_k: int = 3,
) -> str:
    """Build a prompt-injection block from multiple domain collections.

    Args:
        query: natural-language query.
        collections: list of domain collection names to query. Default: all 5.
        max_chars: total max chars for the block.
        top_k: results per collection.

    Returns:
        Formatted string. Empty if no results.
    """
    if not query or not query.strip():
        return ""

    if collections is None:
        # Default: all 5
        collections = [
            NEWS_EVENTS, MARKET_SIGNALS, PAST_DECISIONS,
            CAUSAL_GRAPH_EMBEDDINGS, PRODUCT_CATALOG,
        ]
    collections = [c for c in collections if c]  # filter falsy

    if not collections:
        return ""

    sections: List[str] = []
    total = 0
    for coll_name in collections:
        svc = next((s for s in _SERVICES.values() if s.collection_name == coll_name), None)
        if svc is None:
            continue
        hits = svc.query(query, top_k=top_k)
        if not hits:
            continue
        body = _format_hits(hits)
        if not body:
            continue
        section = f"### {coll_name}\n{body}"
        if total + len(section) + 4 > max_chars:
            break
        sections.append(section)
        total += len(section) + 4

    if not sections:
        return ""
    return "[Domain Knowledge Context]\n" + "\n\n".join(sections)