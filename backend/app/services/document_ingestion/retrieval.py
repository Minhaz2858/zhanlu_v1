"""Retrieval helpers for the document tools.

``search`` returns raw chunks (granular); ``answer`` retrieves then
synthesises a prose answer via a single LLM call. Both are called by
the ``data_agent`` subagent — never by the user-facing agent directly.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase
from app.services.document_ingestion import store
from app.services.sub_agent_reliability import call_llm_with_reliability

logger = logging.getLogger(__name__)


def _load_kb(db: Session, kb_id: str) -> KnowledgeBase | None:
    return (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )


def search(db: Session, kb_id: str, query: str, top_k: int = 5) -> dict:
    """Vector search. Returns ``{"success", "chunks", "source_id", "source_name"}``."""
    kb = _load_kb(db, kb_id)
    if kb is None:
        return {"success": False, "error": f"KnowledgeBase {kb_id!r} not found"}
    if kb.source_kind != "file":
        return {
            "success": False,
            "error": (
                f"KnowledgeBase {kb_id!r} is source_kind={kb.source_kind!r}, "
                f"not 'file'. Use the database tools instead."
            ),
        }
    if kb.indexing_status != "ready":
        return {
            "success": False,
            "error": (
                f"Document index is not ready (status={kb.indexing_status!r}). "
                f"Wait for indexing to finish or re-trigger it from My Space."
            ),
        }
    top_k = max(1, min(int(top_k), 20))
    res = store.query(
        org_id=kb.org_id, kb_ids=[kb_id], query_text=query, top_k=top_k
    )
    return {
        "success": True,
        "chunks": res["chunks"],
        "source_id": kb.id,
        "source_name": kb.name,
        "file_name": kb.file_url or "",
    }


async def answer(db: Session, kb_id: str, question: str) -> dict:
    """Vector search + LLM synthesis -> prose answer with citations."""
    top_k = 6
    sr = search(db, kb_id, question, top_k=top_k)
    if not sr.get("success"):
        return sr
    chunks = sr["chunks"]
    if not chunks:
        return {
            "success": True,
            "answer": (
                f"No relevant passages found in {sr['source_name']!r} "
                f"for that question."
            ),
            "chunks": [],
            "source_id": kb_id,
            "source_name": sr["source_name"],
        }

    context_block = "\n\n".join(
        f"[{i + 1}] (score={c['score']:.3f}, file={c['file_name']}, "
        f"chunk={c['chunk_index']})\n{c['text']}"
        for i, c in enumerate(chunks)
    )
    prompt = (
        "Answer the user's question using ONLY the passages below. "
        "Cite passages by their [N] index. If the passages don't contain "
        "the answer, say so explicitly — do not fabricate.\n\n"
        f"PASSAGES:\n{context_block}\n\n"
        f"QUESTION: {question}\n\nANSWER:"
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        resp = await call_llm_with_reliability(messages, tools=[], temperature=0.2)
        prose = (resp.get("content") or "").strip()
    except Exception as e:
        logger.warning("answer_from_documents synthesis failed: %s", e)
        prose = (
            f"Found {len(chunks)} relevant passage(s) in {sr['source_name']!r} "
            f"but could not synthesise an answer ({e})."
        )

    return {
        "success": True,
        "answer": prose,
        "chunks": chunks,
        "source_id": kb_id,
        "source_name": sr["source_name"],
        "citations": [
            {
                "file_name": c["file_name"],
                "chunk_index": c["chunk_index"],
                "score": c["score"],
            }
            for c in chunks
        ],
    }
