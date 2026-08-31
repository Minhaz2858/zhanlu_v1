"""RAG retriever wrapper: query the Chroma knowledge base and return scored chunks.

Public API:
    retrieve(query, n_results=3, collection="industry_reports")
        # Simple text-only results, useful when the caller doesn't care about
        # distances or metadata.
    retrieve_rich(query, n_results=3, collection="industry_reports")
        # Returns RetrievedChunk objects with metadata + normalized scores.
    retrieve_with_context(query, n_results=3, max_chars=1800, collection=...)
        # Returns a formatted multi-line string suitable for LLM prompt injection.

RAG retriever adapted to zhanlu's
``app.services.rag.knowledge_base.create_knowledge_base()`` factory.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, List

from app.services.rag.collection_names import (
    ALL_COLLECTION_NAMES,
    INDUSTRY_REPORTS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


#: All semantic collections the retriever can target.
KNOWN_COLLECTIONS: List[str] = list(ALL_COLLECTION_NAMES)

#: Default collection for retrieval when the caller doesn't specify.
DEFAULT_COLLECTION: str = INDUSTRY_REPORTS


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class RetrievedChunk:
    """A single retrieved document chunk with its metadata and relevance score."""

    text: str
    source: str  # e.g. "docs/EDIA_AI_REBUILD_SPEC.md"
    file_stem: str  # e.g. "EDIA_AI_REBUILD_SPEC"
    chunk_index: int
    total_chunks: int
    score: float = 0.0  # Normalized 0–1 relevance (1 = most relevant)
    source_label: str = ""
    extra_metadata: dict = field(default_factory=dict)

    def formatted(self, max_chars: int = 600) -> str:
        """Return a compact single-line representation for prompt injection."""
        excerpt = (self.text or "")[:max_chars].replace("\n", " ").strip()
        if len(self.text or "") > max_chars:
            excerpt += "…"
        label = self.source_label or self.source or "unknown"
        return f"[Source: {label}]({self.chunk_index + 1}/{self.total_chunks}) {excerpt}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_scores(results: List[dict]) -> List[dict]:
    """Convert Chroma distances to 0–1 relevance scores (higher = more relevant)."""
    if not results:
        return results
    distances = [float(r.get("distance", 0.0)) for r in results]
    if not distances:
        return results
    min_d, max_d = min(distances), max(distances)
    span = max_d - min_d
    if span <= 0:
        for r in results:
            r["score"] = 1.0
    else:
        for r in results:
            r["score"] = math.exp(-float(r.get("distance", 0.0)))
    return results


def _get_kb() -> Any:
    """Return the best-available knowledge base for the default org.

    Imports lazily so the retriever module is importable even when ChromaDB
    is not available.
    """
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


def _resolve_collection(kb: Any, collection: str) -> Any:
    """Map a collection name to a ChromaDB collection object on ``kb``.

    Falls back to the default collection if the named one is not found.
    """
    if kb is None:
        return None
    if hasattr(kb, "get_collection"):
        try:
            return kb.get_collection(collection)
        except Exception:  # noqa: BLE001
            pass
    if hasattr(kb, "collection") and collection in (DEFAULT_COLLECTION, INDUSTRY_REPORTS, ""):
        return kb.collection
    return None


def _kb_hits_to_chunks(hits: List[dict]) -> List[RetrievedChunk]:
    """Convert a list of KB hit dicts to RetrievedChunk objects."""
    out: List[RetrievedChunk] = []
    for hit in hits:
        meta = hit.get("metadata", {}) or {}
        out.append(RetrievedChunk(
            text=hit.get("text", "") or "",
            source=str(meta.get("source", "")),
            file_stem=str(meta.get("file_stem", "unknown")),
            chunk_index=int(meta.get("chunk_index", 0)),
            total_chunks=int(meta.get("total_chunks", 1)),
            score=float(hit.get("score", 0.0)),
            source_label=str(meta.get("source_label", meta.get("source", ""))),
            extra_metadata=dict(meta),
        ))
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def retrieve(
    query: str,
    n_results: int = 3,
    collection: str = DEFAULT_COLLECTION,
) -> List[RetrievedChunk]:
    """Query the RAG knowledge base and return the top-n scored chunks.

    Args:
        query: Natural-language query string.
        n_results: Number of chunks to retrieve (default 3).
        collection: One of ``KNOWN_COLLECTIONS``.

    Returns:
        List of ``RetrievedChunk`` objects sorted by descending relevance score.
        Returns an empty list on any error or empty query.
    """
    if not query or not query.strip():
        return []

    kb = _get_kb()
    if kb is None:
        return []

    coll = _resolve_collection(kb, collection) or _resolve_collection(kb, DEFAULT_COLLECTION)
    if coll is None:
        return []

    try:
        result = coll.query(
            query_texts=[query],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("retrieve: coll.query failed: %s", exc)
        return []

    raw_docs = (result.get("documents") or [[]])[0]
    raw_metas = (result.get("metadatas") or [[]])[0]
    raw_dists = (result.get("distances") or [[]])[0]

    hits: List[dict] = []
    for doc, meta, dist in zip(raw_docs, raw_metas, raw_dists):
        if not doc:
            continue
        hits.append({
            "text": doc,
            "metadata": meta or {},
            "distance": float(dist) if dist is not None else 0.0,
        })
    hits = _normalize_scores(hits)
    return _kb_hits_to_chunks(hits)


def retrieve_rich(
    query: str,
    n_results: int = 3,
    collection: str = DEFAULT_COLLECTION,
) -> List[RetrievedChunk]:
    """Query with full metadata and scores; prefers hybrid (dense+lexical) when available."""
    if not query or not query.strip():
        return []

    kb = _get_kb()
    if kb is None:
        return []

    if hasattr(kb, "hybrid_query"):
        try:
            hybrid_hits = kb.hybrid_query(
                name=collection,
                query_text=query,
                top_k=n_results,
            )
            if hybrid_hits:
                return _kb_hits_to_chunks(hybrid_hits)
        except Exception as exc:  # noqa: BLE001
            logger.debug("retrieve_rich: hybrid_query failed: %s", exc)

    return retrieve(query, n_results=n_results, collection=collection)


def retrieve_with_context(
    query: str,
    n_results: int = 3,
    max_chars: int = 1800,
    collection: str = DEFAULT_COLLECTION,
) -> str:
    """Build a RAG context string for injection into LLM prompts.

    Returns empty string if no relevant results found.
    """
    chunks = retrieve_rich(query, n_results=n_results, collection=collection)
    if not chunks:
        return ""

    total_chars = 0
    lines: List[str] = []
    for chunk in chunks:
        formatted = chunk.formatted(max_chars=500)
        if total_chars + len(formatted) + 5 > max_chars:
            break
        lines.append(formatted)
        total_chars += len(formatted) + 5

    if not lines:
        return ""

    header = f"[Knowledge Base - Top {len(lines)} Results]"
    return header + "\n" + "\n---\n".join(lines)
