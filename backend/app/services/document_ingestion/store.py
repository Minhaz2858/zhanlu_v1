"""ChromaDB vector store — one collection per org_id.

Persistence dir is ``backend/data/chroma/`` (configurable via the
``CHROMA_DIR`` env var). Collection name is ``kb_{org_id}`` so all KBs
in one org share an index, but chunks are tagged with their ``kb_id``
in metadata, allowing per-KB delete + filtered query.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any

from app.services.document_ingestion import embedder

logger = logging.getLogger(__name__)

_CHROMA_DIR = os.environ.get(
    "CHROMA_DIR", str(Path(__file__).resolve().parents[3] / "data" / "chroma")
)

_CLIENT: Any = None


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

        Path(_CHROMA_DIR).mkdir(parents=True, exist_ok=True)
        _CLIENT = chromadb.PersistentClient(path=_CHROMA_DIR)
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
        return int(coll.count(where={"kb_id": kb_id}))
    except Exception:
        return 0


def reset_for_tests() -> None:
    """Test-only: clear the cached client."""
    global _CLIENT
    _CLIENT = None
