"""ChromaDB vector store — multi-collection, hybrid (dense+lexical) retrieval.

Persistence dir is ``backend/data/chroma/`` (configurable via the
``CHROMA_DIR`` env var).

This module supports two deployment modes:

1. **Legacy KB-per-org** (backward compatible):
   - One collection per org_id named ``kb_{org_id}``.
   - Module-level helpers: ``upsert_chunks``, ``query``, ``delete_kb``,
     ``count``. Unchanged signatures; safe to call from existing callers.

2. **Multi-collection per org** (hybrid RAG):
   - 9 semantic collections per org (industry_reports, weekly_reports,
     past_decisions, …) named ``domain_{org_id}_{collection_name}``.
   - Use ``CollectionStore`` to operate on a single collection.
   - Use ``MultiCollectionStore`` to manage all 9 in one object.
   - Use ``hybrid_query()`` to do dense + lexical + RRF fusion.

The two modes coexist: the legacy collection ``kb_{org_id}`` is independent
from the ``domain_*`` collections.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.services.document_ingestion import embedder
from app.services.rag.hybrid_retrieval import (
    DEFAULT_DENSE_WEIGHT,
    DEFAULT_RRF_K,
    DEFAULT_SPARSE_WEIGHT,
    query_terms,
    sparse_hits_from_collection,
)

logger = logging.getLogger(__name__)

_DEFAULT_CHROMA_DIR = str(
    Path(__file__).resolve().parents[3] / "data" / "chroma"
)


def _chroma_dir() -> str:
    """Resolve CHROMA_DIR at call time (so tests can monkeypatch the env)."""
    return os.environ.get("CHROMA_DIR", _DEFAULT_CHROMA_DIR)


_CLIENT: Any = None

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-\.]*")


class _NoopEmbeddingFunction:
    """Dummy embedding function so Chroma never loads sentence-transformers.

    We always pass ``embeddings=`` to ``upsert`` and ``query_embeddings=``
    to ``query`` — Chroma's own embedding function is never called. But
    Chroma still requires one to be attached to the collection, and its
    default would pull in the real model. This no-op satisfies the API
    without loading anything heavy.
    """

    def __init__(self):
        pass

    def __call__(self, input):  # noqa: A002 — chroma's expected signature
        raise RuntimeError(
            "_NoopEmbeddingFunction should never be called — "
            "embeddings are always passed explicitly."
        )

    def name(self):
        return "noop"


def _get_client() -> Any:
    global _CLIENT
    if _CLIENT is None:
        import chromadb  # lazy

        d = _chroma_dir()
        Path(d).mkdir(parents=True, exist_ok=True)
        _CLIENT = chromadb.PersistentClient(path=d)
    return _CLIENT


def _collection_name(org_id: str) -> str:
    return f"kb_{org_id}"


def _get_collection(org_id: str) -> Any:
    client = _get_client()
    return client.get_or_create_collection(
        name=_collection_name(org_id),
        metadata={"hnsw:space": "cosine"},
        embedding_function=_NoopEmbeddingFunction(),
    )


# ---------------------------------------------------------------------------
# Lexical tokenization (re-exported for tests)
# ---------------------------------------------------------------------------


def tokenize_lexical(text: str) -> List[str]:
    """Tokenize text for lexical/BM25 search: ASCII words + CJK bigrams.

    Thin re-export of ``hybrid_retrieval.query_terms`` (returns list, not set,
    for callers that want ordered tokens). Equivalent algorithm:
    - CJK runs (consecutive Han chars) → sliding bigrams
    - ASCII alphanumeric tokens → lowercased
    - Singleton CJK chars excluded
    """
    return sorted(query_terms(text))


# ---------------------------------------------------------------------------
# RRF fusion helper (exposed for testing in isolation)
# ---------------------------------------------------------------------------


def rrf_fuse(
    dense_hits: Sequence[Dict[str, Any]],
    sparse_hits: Sequence[Dict[str, Any]],
    k: int = DEFAULT_RRF_K,
) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion of two ranked hit lists.

    Each input is a list of dicts that contain at least an ``"id"`` key
    (and arbitrary other keys). Output is a list of dicts sorted by
    descending RRF score, each augmented with ``"score"`` (the fused
    RRF weight) and the original hit's fields. Items present in only
    one list still get a score from the list they appeared in.

    Args:
        dense_hits: ranked list of dicts with ``"id"`` key (vector results).
        sparse_hits: ranked list of dicts with ``"id"`` key (lexical results).
        k: RRF constant (Cormack et al., 2009). Higher = more weight to
           lower-ranked items. Default 60.

    Returns:
        Fused list sorted by descending score, each entry has ``"id"``,
        ``"score"``, and any other fields from the source hit dicts.
    """
    score_map: Dict[str, float] = {}
    payload: Dict[str, Dict[str, Any]] = {}

    for rank, hit in enumerate(dense_hits, start=1):
        doc_id = hit.get("id")
        if doc_id is None:
            continue
        score_map[doc_id] = score_map.get(doc_id, 0.0) + 1.0 / (k + rank)
        if doc_id not in payload:
            payload[doc_id] = {k_: v for k_, v in hit.items() if k_ != "score"}

    for rank, hit in enumerate(sparse_hits, start=1):
        doc_id = hit.get("id")
        if doc_id is None:
            continue
        score_map[doc_id] = score_map.get(doc_id, 0.0) + 1.0 / (k + rank)
        if doc_id not in payload:
            payload[doc_id] = {k_: v for k_, v in hit.items() if k_ != "score"}

    fused: List[Dict[str, Any]] = []
    for doc_id, fused_score in score_map.items():
        entry = dict(payload[doc_id])
        entry["id"] = doc_id
        entry["score"] = fused_score
        fused.append(entry)

    fused.sort(key=lambda x: x["score"], reverse=True)
    return fused


# ---------------------------------------------------------------------------
# CollectionStore — wraps a single named collection
# ---------------------------------------------------------------------------


class CollectionStore:
    """Operate on a single ChromaDB collection with hybrid (dense+lexical) RAG.

    The collection name is fully qualified at construction time. By default
    the store derives ``domain_{org_id}_{collection_name}``; the legacy
    ``kb_{org_id}`` layout is used when ``collection_name`` is None or empty.

    Args:
        org_id: organization / tenant identifier.
        collection_name: one of the 9 ALL_COLLECTION_NAMES, or None/empty
            for the legacy ``kb_{org_id}`` collection.
        embedding_fn: ChromaDB-compatible embedding function. Must implement
            ``embed_documents(texts)`` and ``embed_query(input)``. If None,
            the module-level ``embedder`` is used.
        client: optional pre-built ChromaDB client (for tests).
    """

    def __init__(
        self,
        org_id: str,
        collection_name: Optional[str] = None,
        embedding_fn: Any = None,
        client: Any = None,
    ) -> None:
        self.org_id = org_id
        self.collection_name = collection_name or ""
        self.embedding_fn = embedding_fn
        self._client_override = client
        if collection_name:
            from app.services.rag.collection_names import (
                build_domain_collection_name,
            )
            self._full_name = build_domain_collection_name(org_id, collection_name)
        else:
            self._full_name = _collection_name(org_id)

    def _client(self) -> Any:
        return self._client_override or _get_client()

    def _coll(self) -> Any:
        emb = self.embedding_fn or _NoopEmbeddingFunction()
        return self._client().get_or_create_collection(
            name=self._full_name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=emb,
        )

    def _embed_one(self, text: str) -> List[float]:
        if self.embedding_fn is not None:
            out = self.embedding_fn.embed_documents([text])
            return list(out[0]) if out else []
        return list(embedder.embed_query(text).tolist())

    @staticmethod
    def _safe_meta(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Ensure metadata is non-empty (ChromaDB 1.5+ requirement)."""
        meta = dict(metadata) if metadata else {}
        if not meta:
            meta = {"_indexed_at": "auto"}
        return meta

    def upsert(
        self,
        doc_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert or update one document."""
        if not text:
            return
        vec = self._embed_one(text)
        coll = self._coll()
        meta = self._safe_meta(metadata)
        coll.upsert(
            ids=[doc_id],
            embeddings=[vec],
            documents=[text],
            metadatas=[meta],
        )

    def count(self, where: Optional[Dict[str, Any]] = None) -> int:
        """Return number of documents in the collection (optionally filtered)."""
        coll = self._coll()
        try:
            if where:
                return int(coll.count(where=where))
            return int(coll.count())
        except Exception:
            return 0

    def delete(
        self,
        doc_id: Optional[str] = None,
        where: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Delete one document by id, or by where-filter."""
        coll = self._coll()
        if doc_id is not None:
            coll.delete(ids=[doc_id])
        elif where is not None:
            coll.delete(where=where)

    def query(
        self,
        query_text: str,
        top_k: int = 5,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Dense vector search only.

        Returns ``[{"id","text","score","metadata"}]``.
        """
        if not query_text:
            return []
        vec = self._embed_one(query_text)
        coll = self._coll()
        kwargs: Dict[str, Any] = {
            "query_embeddings": [vec],
            "n_results": max(1, top_k),
        }
        if where is not None:
            kwargs["where"] = where
        try:
            res = coll.query(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CollectionStore.query failed: %s", exc)
            return []

        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]

        out: List[Dict[str, Any]] = []
        for doc_id, doc_text, dist, meta in zip(ids, docs, dists, metas):
            try:
                score = 1.0 - float(dist) if dist is not None else 0.0
            except (TypeError, ValueError):
                score = 0.0
            out.append({
                "id": doc_id,
                "text": doc_text or "",
                "score": score,
                "metadata": meta or {},
            })
        return out


# ---------------------------------------------------------------------------
# MultiCollectionStore — registry of per-collection stores
# ---------------------------------------------------------------------------


class MultiCollectionStore:
    """Owns one CollectionStore per semantic collection for a single org.

    Use ``get_or_create(name)`` to lazily instantiate / re-use a
    CollectionStore for each of the semantic collections.
    """

    def __init__(
        self,
        org_id: str,
        embedding_fn: Any = None,
        client: Any = None,
    ) -> None:
        self.org_id = org_id
        self.embedding_fn = embedding_fn
        self._client_override = client
        self._stores: Dict[str, CollectionStore] = {}

    @property
    def collections(self) -> Dict[str, CollectionStore]:
        """Live registry of CollectionStore instances keyed by collection name."""
        return dict(self._stores)

    def get_or_create(self, collection_name: str) -> CollectionStore:
        """Get the CollectionStore for ``collection_name``, creating it if needed."""
        if collection_name not in self._stores:
            self._stores[collection_name] = CollectionStore(
                org_id=self.org_id,
                collection_name=collection_name,
                embedding_fn=self.embedding_fn,
                client=self._client_override,
            )
        return self._stores[collection_name]

    def all_collection_names(self) -> List[str]:
        from app.services.rag.collection_names import ALL_COLLECTION_NAMES
        return list(ALL_COLLECTION_NAMES)


# ---------------------------------------------------------------------------
# hybrid_query — RRF fusion of dense + lexical on a single collection
# ---------------------------------------------------------------------------


def hybrid_query(
    collection_store: CollectionStore,
    query_text: str,
    top_k: int = 5,
    where: Optional[Dict[str, Any]] = None,
    rrf_k: int = DEFAULT_RRF_K,
    dense_weight: float = DEFAULT_DENSE_WEIGHT,
    sparse_weight: float = DEFAULT_SPARSE_WEIGHT,
) -> List[Dict[str, Any]]:
    """Run a hybrid (dense + lexical) search on a single collection.

    Steps:
      1. Dense vector search via ``CollectionStore.query`` (top_k * 2 for RRF headroom).
      2. Lexical search via ``sparse_hits_from_collection`` (top_k * 2).
      3. RRF fusion of both lists with k=60.
      4. Return top_k results, each with ``id``, ``text``, ``score``, ``metadata``,
         plus the source (``"dense"`` / ``"sparse"`` / ``"both"``).

    Args:
        collection_store: the CollectionStore to query.
        query_text: raw user query (any length).
        top_k: how many final results to return.
        where: optional ChromaDB ``where`` filter.
        rrf_k: RRF constant (default 60).
        dense_weight: currently informational (RRF is unweighted by default).
        sparse_weight: currently informational.

    Returns:
        List of dicts sorted by descending RRF score.
    """
    if not query_text:
        return []

    fetch_k = max(top_k * 2, 10)

    # 1. Dense hits (with metadata so we can echo it back in the result)
    dense_raw = collection_store.query(query_text, top_k=fetch_k, where=where)
    dense_hits: List[Dict[str, Any]] = []
    for hit in dense_raw:
        dense_hits.append({
            "id": hit.get("id"),
            "text": hit.get("text", ""),
            "score": hit.get("score", 0.0),
            "metadata": hit.get("metadata", {}),
            "source": "dense",
        })

    # 2. Lexical hits — use raw Chroma collection for ``sparse_hits_from_collection``
    sparse_hits: List[Dict[str, Any]] = []
    try:
        coll = collection_store._coll()
        sparse_pairs = sparse_hits_from_collection(
            coll, query_text, top_k=fetch_k, prefetch_limit=0, where=where
        )
        if sparse_pairs:
            payload = coll.get(ids=[p[0] for p in sparse_pairs])
            ids_back = payload.get("ids", []) or []
            docs_back = payload.get("documents", []) or []
            metas_back = payload.get("metadatas", []) or []
            lookup = {i: idx for idx, i in enumerate(ids_back)}
            for doc_id, score in sparse_pairs:
                if doc_id in lookup:
                    i = lookup[doc_id]
                    sparse_hits.append({
                        "id": doc_id,
                        "text": docs_back[i] if i < len(docs_back) else "",
                        "score": float(score),
                        "metadata": metas_back[i] if i < len(metas_back) else {},
                        "source": "sparse",
                    })
    except Exception as exc:  # noqa: BLE001
        logger.debug("hybrid_query sparse fetch failed: %s", exc)

    # 3. RRF fusion
    fused = rrf_fuse(dense_hits, sparse_hits, k=rrf_k)

    # 4. Tag source + cap at top_k
    dense_ids = {h["id"] for h in dense_hits}
    sparse_ids = {h["id"] for h in sparse_hits}
    for hit in fused:
        in_d = hit["id"] in dense_ids
        in_s = hit["id"] in sparse_ids
        hit["source"] = (
            "both" if (in_d and in_s) else ("dense" if in_d else "sparse")
        )
    return fused[:top_k]


# ---------------------------------------------------------------------------
# Legacy module-level API (unchanged signatures — backward compatible)
# ---------------------------------------------------------------------------


def upsert_chunks(
    org_id: str,
    kb_id: str,
    chunks: list[dict],
    metas: list[dict],
) -> int:
    """Embed + upsert chunks for one KB. Returns count stored."""
    if not chunks:
        return 0
    texts = [c["text"] for c in chunks]
    vecs = embedder.embed_texts(texts)
    coll = _get_collection(org_id)
    ids = [f"{kb_id}_{c['index']}_{uuid.uuid4().hex[:8]}" for c in chunks]
    metadatas = [
        {
            "kb_id": kb_id,
            "chunk_index": c["index"],
            "file_name": m.get("file_name", ""),
            "file_type": m.get("file_type", ""),
        }
        for c, m in zip(chunks, metas)
    ]
    coll.upsert(ids=ids, embeddings=vecs.tolist(), documents=texts, metadatas=metadatas)
    return len(ids)


def delete_kb(org_id: str, kb_id: str) -> None:
    """Delete all chunks for one KB."""
    coll = _get_collection(org_id)
    coll.delete(where={"kb_id": kb_id})


def query(
    org_id: str,
    kb_ids: list[str],
    query_text: str,
    top_k: int = 5,
) -> dict:
    """Vector search across one or more KBs in the org.

    Returns ``{"chunks": [{"text","score","kb_id","file_name",
    "file_type","chunk_index"}]}`` sorted by score desc.
    """
    if not kb_ids:
        return {"chunks": []}
    qvec = embedder.embed_query(query_text).tolist()
    coll = _get_collection(org_id)
    res = coll.query(
        query_embeddings=[qvec],
        n_results=top_k * len(kb_ids),
        where={"kb_id": {"$in": list(kb_ids)}},
    )
    out: list[dict] = []
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    for doc, meta, dist in zip(docs, metas, dists):
        out.append({
            "text": doc,
            "score": 1.0 - float(dist),  # cosine distance → similarity
            "kb_id": meta.get("kb_id"),
            "file_name": meta.get("file_name", ""),
            "file_type": meta.get("file_type", ""),
            "chunk_index": meta.get("chunk_index", 0),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return {"chunks": out[:top_k]}


def count(org_id: str, kb_id: str) -> int:
    """Return chunk count for one KB."""
    coll = _get_collection(org_id)
    try:
        # ChromaDB's ``count()`` does not accept a ``where`` filter; use
        # ``get()`` to fetch only matching ids and count them.
        payload = coll.get(where={"kb_id": kb_id})
        return len(payload.get("ids", []) or [])
    except Exception:
        return 0


def reset_for_tests() -> None:
    """Test-only: clear the cached client."""
    global _CLIENT
    _CLIENT = None
