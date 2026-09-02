"""RAG over chat uploads — session-scoped retrieval for large/many files.

The chat loop previously dumped every uploaded file's extracted text into
the prompt, capped at MAX_EXTRACTED_CHARS (~30k tokens) per file and
MAX_HISTORICAL_EXTRACT_CHARS across the conversation. Beyond that wall the
agent simply could not answer across big documents ("answer across 200
contracts" was impossible).

This module routes LARGE uploads (and many-file turns) through the SAME
ChromaDB + local-embedding pipeline the KnowledgeBase path uses:

    index_upload_text(...)      chunk -> embed -> upsert (idempotent)
    retrieve_upload_chunks(...) embed the question -> top-k chunks

Design rules (mirror the KB path and the chat upload pipeline):

- Session-scoped: chunks live in one Chroma collection
  ``domain_{org_id}_chat_uploads`` with ``session_id`` metadata; queries
  filter by session so conversations never see each other's files.
- Idempotent indexing: re-indexing the same (session, file_url) deletes
  the old chunks first, so re-uploads and repeated turns never duplicate.
- Fail-open: any embedding/store error logs and returns empty — callers
  fall back to the existing plain-text dump. This feature NEVER breaks a
  turn.
- Local embeddings only: MiniLM-L6-v2 via sentence-transformers, cached
  under /app/data/hf_cache. No cloud dependency (customers refuse cloud).
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Patchable in tests (monkeypatch ``_get_store`` or ``_availability_probe``).
_STORE_CACHE: dict[str, Any] = {}
_STORE_LOCK = threading.Lock()
_AVAILABILITY: Optional[bool] = None
_AVAILABILITY_LOCK = threading.Lock()


def _store(org_id: str, embedding_fn: Any = None, client: Any = None) -> Any:
    """Return a cached CollectionStore for the chat-upload collection.

    ``embedding_fn`` / ``client`` are test-only overrides (mirrors the
    CollectionStore constructor's own overrides).
    """
    key = f"{org_id}|{id(embedding_fn)}|{id(client)}"
    with _STORE_LOCK:
        if key not in _STORE_CACHE:
            from app.services.document_ingestion.store import CollectionStore

            _STORE_CACHE[key] = CollectionStore(
                org_id=org_id,
                collection_name=settings.RAG_UPLOADS_COLLECTION,
                embedding_fn=embedding_fn,
                client=client,
            )
        return _STORE_CACHE[key]


def availability() -> bool:
    """True when the RAG pipeline is likely usable. Probed once per process.

    Conservative: if the probe itself fails, we report unavailable so the
    chat loop degrades to the text dump instead of paying per-turn latency
    to discover Chroma is broken.
    """
    global _AVAILABILITY
    with _AVAILABILITY_LOCK:
        if _AVAILABILITY is not None:
            return _AVAILABILITY
        ok = False
        try:
            if not settings.RAG_UPLOADS_ENABLED:
                ok = False
            else:
                from app.services.document_ingestion import embedder  # noqa: F401
                from app.services.document_ingestion import store  # noqa: F401

                _store("__probe__")
                ok = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("upload_rag availability probe failed: %s", exc)
            ok = False
        _AVAILABILITY = ok
        return _AVAILABILITY


def reset_for_tests() -> None:
    """Clear caches (test isolation)."""
    global _AVAILABILITY
    with _STORE_LOCK:
        _STORE_CACHE.clear()
    with _AVAILABILITY_LOCK:
        _AVAILABILITY = None


def _doc_id(session_id: str, file_url: str, chunk_index: int) -> str:
    """Stable Chroma id so re-indexing the same file overwrites, not appends."""
    raw = f"{session_id}|{file_url}|{chunk_index}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def index_upload_text(
    file_url: str,
    session_id: str,
    org_id: str,
    text: str,
    file_name: str = "",
    agent: str = "",
    project_id: str = "",
    project_name: str = "",
) -> int:
    """Chunk + embed + upsert one uploaded file's text for a session.

    Idempotent: existing chunks for (session_id, file_url) are deleted
    first, so a re-upload or a repeated turn refreshes rather than
    duplicates. Returns the number of chunks stored, or 0 on any failure
    (never raises).

    Isolation scope (full stack, all stored as Chroma metadata so
    retrieval can filter on any level):
        org (collection) -> agent -> project -> session -> file
    ``agent`` / ``project_id`` may be empty for global/unscoped chats;
    retrieval filters only on non-empty values.
    """
    if not text or not text.strip():
        return 0
    try:
        from app.services.document_ingestion import chunker

        chunks = chunker.chunk_text(text, max_tokens=800, overlap=100)
        if not chunks:
            return 0
        coll = _store(org_id)
        # Refresh, don't accumulate. Chroma where-filters with multiple keys
        # require an explicit $and expression.
        coll.delete(
            where={
                "$and": [
                    {"session_id": session_id},
                    {"file_url": file_url},
                ]
            }
        )
        for c in chunks:
            idx = int(c.get("index", 0))
            coll.upsert(
                doc_id=_doc_id(session_id, file_url, idx),
                text=c["text"],
                metadata={
                    "session_id": session_id,
                    "file_url": file_url,
                    "file_name": file_name,
                    "chunk_index": idx,
                    "source": "chat_upload",
                    "agent": agent or "",
                    "project_id": project_id or "",
                    "project_name": project_name or "",
                },
            )
        return len(chunks)
    except Exception as exc:  # noqa: BLE001
        logger.warning("upload_rag index failed for %s: %s", file_url, exc)
        return 0


def _scope_where(session_id: str = "", agent: str = "", project_id: str = "") -> dict:
    """Build a Chroma where-filter from the non-empty scope levels."""
    clauses: list[dict] = []
    if session_id:
        clauses.append({"session_id": session_id})
    if agent:
        clauses.append({"agent": agent})
    if project_id:
        clauses.append({"project_id": project_id})
    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def retrieve_upload_chunks(
    session_id: str,
    org_id: str,
    query: str,
    top_k: int | None = None,
    agent: str = "",
    project_id: str = "",
) -> list[dict]:
    """Embed ``query`` and return the top-k chunks for the given scope.

    Filters on session (plus agent/project when provided) so chunks never
    leak across conversations, agents, or projects. Returns
    ``[{"text", "score", "metadata"}]`` ordered by relevance, or ``[]`` on
    any failure (never raises).
    """
    if not query or not query.strip():
        return []
    try:
        k = top_k or settings.RAG_UPLOADS_TOP_K
        coll = _store(org_id)
        where = _scope_where(session_id=session_id, agent=agent, project_id=project_id)
        kwargs: dict = {"top_k": max(1, min(int(k), 20))}
        if where:
            kwargs["where"] = where
        return coll.query(query_text=query, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("upload_rag retrieve failed for scope %s: %s", session_id, exc)
        return []


def retrieve_project_chunks(
    project_id: str,
    org_id: str,
    query: str,
    top_k: int | None = None,
    agent: str = "",
) -> list[dict]:
    """Cross-session retrieval across every chat upload in a project.

    Useful for project-wide questions ("what did we discuss about X across
    all chats in this project?"). Requires ``project_id`` to be non-empty;
    ``agent`` narrows to one agent when provided.
    """
    if not project_id:
        return []
    return retrieve_upload_chunks(
        session_id="",
        org_id=org_id,
        query=query,
        top_k=top_k,
        agent=agent,
        project_id=project_id,
    )


def build_retrieval_block(query: str, chunks: list[dict]) -> str:
    """Format retrieved chunks as a prompt block with cite-by-index.

    The LLM is told to answer from these passages and cite file names —
    the same contract the KB retrieval path uses.
    """
    if not chunks:
        return ""
    passages = []
    for i, c in enumerate(chunks, start=1):
        meta = c.get("metadata") or {}
        fname = meta.get("file_name") or ""
        fname_part = f", file={fname}" if fname else ""
        score = c.get("score")
        score_part = f", score={score:.3f}" if isinstance(score, (int, float)) else ""
        passages.append(
            f"[{i}] (chunk={meta.get('chunk_index', '')}{fname_part}{score_part})\n{c.get('text', '')}"
        )
    return (
        "The uploaded file(s) below were too large to show in full. These "
        "passages were retrieved for your question. Answer using them; cite "
        "the [N] index and file name of the passage you use. If the passages "
        "don't contain the answer, say so — do not fabricate.\n\n"
        f"QUESTION: {query}\n\n"
        + "\n\n".join(passages)
    )
