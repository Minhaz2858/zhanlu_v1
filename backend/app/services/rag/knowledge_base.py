"""Three-tier RAG knowledge base with graceful degradation.

Classes (all share a common interface):

1. ``RAGKnowledgeBase`` — full ChromaDB-backed 9-collection KB
   with dense + lexical + hybrid retrieval.

2. ``LexicalRAGKnowledgeBase`` — in-memory fallback with pure lexical
   retrieval only. Used when ChromaDB cannot be initialized
   (e.g. permission errors, missing persistent directory).

3. ``DisabledRAGKnowledgeBase`` — graceful no-op. All operations
   return empty results; ``upsert`` returns False. Used when
   RAG is explicitly disabled via configuration.

Factory:
    ``create_knowledge_base(org_id, embedding_fn=None, persist_dir=None)``
    inspects runtime availability and returns the best available tier.

Common interface (all three classes):
    list_collections()                            → List[str]
    get_collection(name: str)                     → CollectionLike | None
    upsert(name, doc_id, text, metadata=None)     → bool
    query(name, query_text, top_k=10)             → List[Tuple[str, float]]
    hybrid_query(name, query_text, top_k=10)      → List[Tuple[str, float]]
    close()                                       → None
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import Any, Dict, List, Optional, Protocol, Tuple

from app.services.rag.collection_names import (
    ALL_COLLECTION_NAMES,
    build_domain_collection_name,
    get_collection_spec,
)
from app.services.rag.hybrid_retrieval import (
    DEFAULT_DENSE_WEIGHT,
    DEFAULT_PREFETCH_LIMIT,
    DEFAULT_RRF_K,
    DEFAULT_SPARSE_WEIGHT,
    hybrid_query_collection,
    sparse_hits_from_collection,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class KnowledgeBaseError(Exception):
    """Raised for RAG-knowledge-base operational errors."""


# ---------------------------------------------------------------------------
# Embedding-function protocol (duck-typed, no chromadb import required)
# ---------------------------------------------------------------------------


class _EmbeddingFunction(Protocol):
    """Protocol matching ChromaDB's embedding function interface.

    ChromaDB expects:
        ef(input: List[str]) -> List[List[float]]
        ef.name() -> str (optional but recommended)
    """

    def __call__(self, input: List[str]) -> List[List[float]]: ...
    def name(self) -> str: ...


# ---------------------------------------------------------------------------
# Local hash embedding function (offline fallback)
# ---------------------------------------------------------------------------


class LocalHashEmbeddingFunction:
    """Deterministic, dependency-free embedding function.

    Used as the last-resort fallback when no real embedding model can be
    loaded (offline mode, no GPU, missing dependency). Produces 256-dim
    SHA-256-hashed token vectors with CJK bigram awareness.

    Quality is much lower than neural embeddings, but it is deterministic
    and reproducible, which is enough to keep the dense retrieval path
    functional in degraded environments.
    """

    DIM = 256

    def name(self) -> str:
        return "local-hash-embedding-v1"

    def __call__(self, input: List[str]) -> List[List[float]]:  # noqa: A002
        return [self._embed_one(t) for t in input]

    @staticmethod
    def _embed_one(text: str) -> List[float]:
        import hashlib

        vec = [0.0] * LocalHashEmbeddingFunction.DIM
        if not text:
            return vec
        # Hash each whitespace- and bigram-split token
        tokens: List[str] = []
        # ASCII tokens
        i = 0
        while i < len(text):
            ch = text[i]
            if "0" <= ch <= "9" or "A" <= ch <= "Z" or "a" <= ch <= "z":
                j = i
                while j < len(text) and (
                    "0" <= text[j] <= "9"
                    or "A" <= text[j] <= "Z"
                    or "a" <= text[j] <= "z"
                ):
                    j += 1
                tokens.append(text[i:j].lower())
                i = j
            elif "\u4e00" <= ch <= "\u9fff":
                # Chinese bigram extraction
                if i + 1 < len(text) and "\u4e00" <= text[i + 1] <= "\u9fff":
                    tokens.append(text[i : i + 2])
                i += 1
            else:
                i += 1
        for tok in tokens:
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            # Distribute hash bytes into 4 bucket positions
            for k in range(4):
                bucket = h[k * 2] % LocalHashEmbeddingFunction.DIM
                sign = 1.0 if (h[k * 2 + 1] & 1) else -1.0
                vec[bucket] += sign
        # L2 normalize
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# Full RAGKnowledgeBase — ChromaDB-backed
# ---------------------------------------------------------------------------


class RAGKnowledgeBase:
    """Full 9-collection RAG backed by ChromaDB.

    Each collection is tenant-scoped (per org_id) using the naming
    convention ``domain_{org_id}_{collection_name}``. The dense embedding
    function is provided by the caller (typically a configured
    sentence-transformers / OpenAI-compatible embedding model).
    """

    def __init__(
        self,
        org_id: str,
        embedding_fn: Optional[Any] = None,
        persist_dir: Optional[str] = None,
    ) -> None:
        self.org_id = org_id
        self._embedding_fn: Any = embedding_fn or LocalHashEmbeddingFunction()
        self._persist_dir = persist_dir or os.environ.get(
            "RAG_PERSIST_DIR"
        ) or os.path.join(tempfile.gettempdir(), "zhanlu_rag_chroma")
        os.makedirs(self._persist_dir, exist_ok=True)

        self._collections: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._closed = False

        # Lazily initialize ChromaDB client
        self._client = self._init_client()

        # Pre-create all 9 collections (idempotent)
        for name in ALL_COLLECTION_NAMES:
            self._get_or_create(name)

    # -- public API --

    def list_collections(self) -> List[str]:
        return list(ALL_COLLECTION_NAMES)

    def _collection_name(self, collection_name: str) -> str:
        return build_domain_collection_name(self.org_id, collection_name)

    def get_collection(self, name: str):
        if name not in ALL_COLLECTION_NAMES:
            return None
        return self._get_or_create(name)

    def upsert(
        self,
        name: str,
        doc_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if name not in ALL_COLLECTION_NAMES:
            raise KnowledgeBaseError(
                f"Unknown collection '{name}'. Valid: {ALL_COLLECTION_NAMES}"
            )
        if self._closed:
            raise KnowledgeBaseError("KnowledgeBase is closed")
        coll = self._get_or_create(name)
        try:
            # ChromaDB 1.5+ requires non-empty metadata for upsert.
            # Use a placeholder if none provided.
            meta = dict(metadata) if metadata else {}
            if not meta:
                meta = {"_indexed_at": "auto"}
            coll.upsert(
                ids=[doc_id],
                documents=[text],
                metadatas=[meta],
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "RAGKnowledgeBase.upsert failed for %s/%s: %s", name, doc_id, exc
            )
            return False

    def query(
        self, name: str, query_text: str, top_k: int = 10
    ) -> List[Tuple[str, float]]:
        if name not in ALL_COLLECTION_NAMES:
            raise KnowledgeBaseError(f"Unknown collection '{name}'")
        coll = self._get_or_create(name)
        try:
            result = coll.query(query_texts=[query_text], n_results=top_k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("RAGKnowledgeBase.query failed: %s", exc)
            return []
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        import math

        out: List[Tuple[str, float]] = []
        for doc_id, dist in zip(ids, distances):
            try:
                score = math.exp(-float(dist))
            except (TypeError, ValueError):
                continue
            out.append((doc_id, score))
        out.sort(key=lambda x: -x[1])
        return out[:top_k]

    def hybrid_query(
        self, name: str, query_text: str, top_k: int = 10
    ) -> List[Tuple[str, float]]:
        if name not in ALL_COLLECTION_NAMES:
            raise KnowledgeBaseError(f"Unknown collection '{name}'")
        coll = self._get_or_create(name)
        try:
            return hybrid_query_collection(
                coll,
                query_text,
                top_k=top_k,
                dense_weight=DEFAULT_DENSE_WEIGHT,
                sparse_weight=DEFAULT_SPARSE_WEIGHT,
                prefetch_limit=DEFAULT_PREFETCH_LIMIT,
                rrf_k=DEFAULT_RRF_K,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("RAGKnowledgeBase.hybrid_query failed: %s", exc)
            # Fall back to sparse-only
            try:
                return sparse_hits_from_collection(coll, query_text, top_k=top_k)
            except Exception:
                return []

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._collections.clear()
            # ChromaDB PersistentClient doesn't have an explicit close in 1.5.x;
            # GC will release resources.

    # -- internal --

    def _init_client(self) -> Any:
        try:
            import chromadb
            from chromadb.config import Settings

            client = chromadb.PersistentClient(
                path=self._persist_dir,
                settings=Settings(anonymized_telemetry=False, allow_reset=False),
            )
            return client
        except Exception as exc:  # noqa: BLE001
            raise KnowledgeBaseError(
                f"Failed to initialize ChromaDB at {self._persist_dir}: {exc}"
            ) from exc

    def _get_or_create(self, name: str) -> Any:
        if name in self._collections:
            return self._collections[name]
        with self._lock:
            if name in self._collections:
                return self._collections[name]
            coll_name = self._collection_name(name)
            coll = self._client.get_or_create_collection(
                name=coll_name,
                embedding_function=self._embedding_fn,
                metadata={
                    "spec_name": get_collection_spec(name).name
                    if get_collection_spec(name)
                    else name,
                    "chinese_label": get_collection_spec(name).chinese_label
                    if get_collection_spec(name)
                    else name,
                    "domain": get_collection_spec(name).domain
                    if get_collection_spec(name)
                    else "unknown",
                    "org_id": self.org_id,
                },
            )
            self._collections[name] = coll
            return coll


# ---------------------------------------------------------------------------
# LexicalRAGKnowledgeBase — pure in-memory fallback
# ---------------------------------------------------------------------------


class LexicalRAGKnowledgeBase:
    """Pure in-memory knowledge base with lexical-only retrieval.

    Used when ChromaDB cannot be initialized. Stores all data in-process,
    so any documents added are lost on restart. Retrieval uses the same
    hybrid_retrieval helpers for sparse scoring.
    """

    def __init__(self, org_id: str) -> None:
        self.org_id = org_id
        # name -> dict[doc_id] -> {text, metadata}
        self._stores: Dict[str, Dict[str, Dict[str, Any]]] = {
            name: {} for name in ALL_COLLECTION_NAMES
        }
        self._lock = threading.Lock()

    def list_collections(self) -> List[str]:
        return list(ALL_COLLECTION_NAMES)

    def get_collection(self, name: str):
        if name not in ALL_COLLECTION_NAMES:
            return None
        return _LexicalCollectionAdapter(self, name)

    def upsert(
        self,
        name: str,
        doc_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if name not in ALL_COLLECTION_NAMES:
            raise KnowledgeBaseError(
                f"Unknown collection '{name}'. Valid: {ALL_COLLECTION_NAMES}"
            )
        with self._lock:
            self._stores[name][doc_id] = {
                "text": text,
                "metadata": metadata or {},
            }
            return True

    def query(
        self, name: str, query_text: str, top_k: int = 10
    ) -> List[Tuple[str, float]]:
        if name not in ALL_COLLECTION_NAMES:
            raise KnowledgeBaseError(f"Unknown collection '{name}'")
        coll = self.get_collection(name)
        assert coll is not None
        return sparse_hits_from_collection(coll, query_text, top_k=top_k)

    def hybrid_query(
        self, name: str, query_text: str, top_k: int = 10
    ) -> List[Tuple[str, float]]:
        # No dense index → hybrid == sparse
        return self.query(name, query_text, top_k=top_k)

    def close(self) -> None:
        with self._lock:
            self._stores.clear()


class _LexicalCollectionAdapter:
    """Adapter that exposes a ChromaDB-like interface to hybrid_retrieval.

    Implements ``.get()`` returning {"ids", "documents"} and
    ``.query()`` returning sparse-only results.
    """

    def __init__(self, kb: LexicalRAGKnowledgeBase, name: str) -> None:
        self._kb = kb
        self._name = name

    def get(self) -> Dict[str, List[Any]]:
        items = self._kb._stores[self._name]
        ids = list(items.keys())
        documents = [items[i]["text"] for i in ids]
        return {"ids": ids, "documents": documents}

    def query(self, query_texts: List[str], n_results: int) -> Dict[str, Any]:
        # Lexical-only; ignore embeddings
        from app.services.rag.hybrid_retrieval import sparse_hits_from_collection

        hits = sparse_hits_from_collection(
            self, query_texts[0], top_k=n_results
        )
        return {
            "ids": [[doc_id for doc_id, _ in hits]],
            "distances": [[1.0 - score for _, score in hits]],
        }


# ---------------------------------------------------------------------------
# DisabledRAGKnowledgeBase — graceful no-op
# ---------------------------------------------------------------------------


class DisabledRAGKnowledgeBase:
    """No-op RAG used when RAG_HYBRID_ENABLED=false or fully unavailable.

    All read operations return empty results. All write operations return
    False. ``list_collections`` still returns the 9 standard names so
    downstream code can iterate without special-casing.
    """

    def __init__(self, org_id: str) -> None:
        self.org_id = org_id

    def list_collections(self) -> List[str]:
        return list(ALL_COLLECTION_NAMES)

    def get_collection(self, name: str):
        return None

    def upsert(
        self,
        name: str,
        doc_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        return False

    def query(
        self, name: str, query_text: str, top_k: int = 10
    ) -> List[Tuple[str, float]]:
        return []

    def hybrid_query(
        self, name: str, query_text: str, top_k: int = 10
    ) -> List[Tuple[str, float]]:
        return []

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_knowledge_base(
    org_id: str,
    embedding_fn: Optional[Any] = None,
    persist_dir: Optional[str] = None,
) -> Any:
    """Construct the best available knowledge-base tier.

    Selection order:
        1. RAGKnowledgeBase (ChromaDB PersistentClient)
        2. LexicalRAGKnowledgeBase (in-memory)
        3. DisabledRAGKnowledgeBase (no-op)

    Returns one of the three KB classes. The returned instance always has
    a common interface: list_collections, get_collection, upsert, query,
    hybrid_query, close.
    """
    # 1. Explicit disable
    enabled_env = os.environ.get("RAG_HYBRID_ENABLED", "true").lower()
    if enabled_env in ("false", "0", "no", "off"):
        logger.info("RAG_HYBRID_ENABLED=false → using DisabledRAGKnowledgeBase")
        return DisabledRAGKnowledgeBase(org_id=org_id)

    # 2. Try full ChromaDB-backed KB
    try:
        kb = RAGKnowledgeBase(
            org_id=org_id,
            embedding_fn=embedding_fn,
            persist_dir=persist_dir,
        )
        logger.info(
            "Initialized RAGKnowledgeBase for org=%s at %s",
            org_id,
            kb._persist_dir,
        )
        return kb
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "RAGKnowledgeBase init failed (%s) → falling back to lexical", exc
        )

    # 3. Lexical fallback
    try:
        kb = LexicalRAGKnowledgeBase(org_id=org_id)
        logger.info("Initialized LexicalRAGKnowledgeBase for org=%s", org_id)
        return kb
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "LexicalRAGKnowledgeBase init failed (%s) → using disabled", exc
        )

    # 4. Disabled last-resort
    return DisabledRAGKnowledgeBase(org_id=org_id)
