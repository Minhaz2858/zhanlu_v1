"""Phase 1 → RAG smart indexer — bridges intelligence layer to RAG collections.

Provides a single ``SmartIndexer`` class that wraps the 5 domain
collection services and exposes high-level methods that the Phase 1
intelligence layer can call to mirror its findings into the RAG
collections. This enables RAG to "see" what intelligence has detected,
without duplicating the underlying intelligence logic.

Public API:
    SmartIndexer(org_id="default")
        .index_event(event_dict)      -> bool   # → news_events
        .index_decision(decision_dict) -> bool   # → past_decisions + decision_outcomes
        .index_signal(signal_dict)    -> bool   # → market_signals
        .index_causal_chain(chain_dict) -> bool # → causal_graph_embeddings
        .index_product(product_dict)  -> bool   # → product_catalog

Each ``index_*`` method:
- Accepts either a dict or an ExtractedEvent-like object (duck-typed)
- Returns True on success, False on failure or invalid input
- Never raises (always catches and logs)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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


def _to_dict(maybe_dict_or_obj: Any) -> Dict[str, Any]:
    """Coerce input to a dict — handles ExtractedEvent or plain dicts."""
    if maybe_dict_or_obj is None:
        return {}
    if isinstance(maybe_dict_or_obj, dict):
        return maybe_dict_or_obj
    if hasattr(maybe_dict_or_obj, "to_dict"):
        try:
            return maybe_dict_or_obj.to_dict() or {}
        except Exception:  # noqa: BLE001
            pass
    if hasattr(maybe_dict_or_obj, "__dict__"):
        return dict(maybe_dict_or_obj.__dict__)
    return {}


class SmartIndexer:
    """Bridge service that mirrors Phase 1 outputs into RAG collections."""

    def __init__(self, org_id: Optional[str] = None) -> None:
        self.org_id = org_id or os.environ.get("DEFAULT_ORG_ID", "default")

    # ---- helpers --------------------------------------------------------

    def _kb(self) -> Any:
        try:
            from app.services.rag.knowledge_base import create_knowledge_base
            return create_knowledge_base(org_id=self.org_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("SmartIndexer._kb failed: %s", exc)
            return None

    def _upsert(self, collection_name: str, doc_id: str, text: str,
                metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Try the KB's high-level upsert first; fall back to raw Chroma."""
        if not doc_id or not text:
            return False
        kb = self._kb()
        if kb is None:
            return False
        meta = self._serialize_meta(metadata)
        # High-level path
        if hasattr(kb, "upsert"):
            try:
                return bool(kb.upsert(
                    name=collection_name,
                    doc_id=doc_id,
                    text=text,
                    metadata=meta,
                ))
            except Exception as exc:  # noqa: BLE001
                logger.debug("SmartIndexer kb.upsert failed: %s", exc)
        # Raw path — fetch collection directly via _get_or_create
        if hasattr(kb, "_get_or_create") and hasattr(kb, "_embed_one"):
            try:
                coll = kb._get_or_create(collection_name)
                vec = kb._embed_one(text)
                coll.upsert(
                    ids=[doc_id],
                    embeddings=[vec],
                    documents=[text],
                    metadatas=[meta],
                )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("SmartIndexer raw upsert failed: %s", exc)
        return False

    @staticmethod
    def _serialize_meta(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Coerce metadata values into ChromaDB-compatible scalar types.

        ChromaDB rejects list values in metadata. We serialize lists as
        JSON strings (prefixed with ``json:`` so they can be round-tripped
        if needed) and stringify any other non-scalar value.
        """
        import json

        out: Dict[str, Any] = {}
        for k, v in (metadata or {}).items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                out[k] = v
            elif isinstance(v, list):
                try:
                    out[k] = "json:" + json.dumps(v, ensure_ascii=False)
                except (TypeError, ValueError):
                    out[k] = str(v)
            elif isinstance(v, dict):
                try:
                    out[k] = "json:" + json.dumps(v, ensure_ascii=False)
                except (TypeError, ValueError):
                    out[k] = str(v)
            else:
                out[k] = str(v)
        if not out:
            out["_indexed_at"] = "auto"
        return out

    # ---- public API ----------------------------------------------------

    def index_event(self, event: Any) -> bool:
        """Mirror an extracted event into the ``news_events`` collection."""
        from app.services.rag.collection_names import NEWS_EVENTS

        ev = _to_dict(event)
        event_id = ev.get("event_id") or ev.get("id")
        if not event_id:
            return False
        headline = ev.get("headline") or ev.get("title") or ""
        if not headline:
            return False
        key_info = ev.get("key_information") or ev.get("summary") or ""
        body = f"{headline} — {key_info}" if key_info else headline
        meta = {
            "event_type": ev.get("event_type", ""),
            "impact_magnitude": ev.get("impact_magnitude", 0.0),
            "source_url": ev.get("source_url", ""),
            "source_credibility": ev.get("source_credibility", ""),
            "headline": headline,
        }
        if key_info:
            meta["key_information"] = key_info
        return self._upsert(NEWS_EVENTS, event_id, body, meta)

    def index_decision(self, decision: Any) -> bool:
        """Mirror a decision into ``past_decisions`` (+ optional outcome)."""
        from app.services.rag.collection_names import (
            DECISION_OUTCOMES,
            PAST_DECISIONS,
        )

        d = _to_dict(decision)
        decision_id = d.get("decision_id") or d.get("id")
        if not decision_id:
            return False
        summary = d.get("summary") or d.get("text") or d.get("decision") or ""
        if not summary:
            return False
        meta = {
            "decision_id": decision_id,
            "rationale": d.get("rationale", ""),
            "decided_at": d.get("decided_at", ""),
            "decided_by": d.get("decided_by", ""),
        }
        ok = self._upsert(PAST_DECISIONS, decision_id, summary, meta)

        # Optional outcome
        outcome = d.get("outcome") or d.get("result") or ""
        if outcome:
            outcome_id = d.get("outcome_id") or f"{decision_id}_outcome"
            outcome_meta = {
                "decision_id": decision_id,
                "outcome_id": outcome_id,
                "metric": d.get("metric", ""),
            }
            self._upsert(DECISION_OUTCOMES, outcome_id, outcome, outcome_meta)

        return ok

    def index_signal(self, signal: Any) -> bool:
        """Mirror a market signal into the ``market_signals`` collection."""
        from app.services.rag.collection_names import MARKET_SIGNALS

        s = _to_dict(signal)
        signal_id = s.get("signal_id") or s.get("id")
        if not signal_id:
            return False
        description = s.get("description") or s.get("text") or s.get("signal") or ""
        if not description:
            return False
        meta = {
            "category": s.get("category", ""),
            "strength": s.get("strength", 0.0),
            "detected_at": s.get("detected_at", ""),
        }
        return self._upsert(MARKET_SIGNALS, signal_id, description, meta)

    def index_causal_chain(self, chain: Any) -> bool:
        """Mirror a causal chain into ``causal_graph_embeddings``."""
        from app.services.rag.collection_names import CAUSAL_GRAPH_EMBEDDINGS

        c = _to_dict(chain)
        chain_id = c.get("chain_id") or c.get("id")
        if not chain_id:
            return False
        description = c.get("description") or c.get("text") or ""
        if not description:
            return False
        meta = {
            "nodes": c.get("nodes", []),
            "edges": [list(e) for e in c.get("edges", [])],
            "probability": c.get("probability", 0.0),
        }
        return self._upsert(CAUSAL_GRAPH_EMBEDDINGS, chain_id, description, meta)

    def index_product(self, product: Any) -> bool:
        """Mirror a product into ``product_catalog``."""
        from app.services.rag.collection_names import PRODUCT_CATALOG

        p = _to_dict(product)
        product_id = p.get("product_id") or p.get("id")
        if not product_id:
            return False
        name = p.get("name") or ""
        if not name:
            return False
        aliases = p.get("aliases") or []
        category = p.get("category", "")
        body = (
            f"{name} ({category}) — aliases: {' | '.join(aliases)}"
            if aliases
            else f"{name} ({category})"
        )
        meta = {
            "name": name,
            "aliases": aliases,
            "category": category,
        }
        return self._upsert(PRODUCT_CATALOG, product_id, body, meta)