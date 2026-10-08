"""Retrieval helpers for the document tools.

``search`` returns raw chunks (granular); ``answer`` retrieves then
synthesises a prose answer via a single LLM call. Both are called by
the ``data_agent`` subagent — never by the user-facing agent directly.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.config import settings
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

    # P2-6 semantic cache: identical/near-identical questions hit memory
    from app.services.document_ingestion.agentic_rag import cache

    cached = cache.lookup(query, [kb_id])
    if cached is not None:
        return {
            "success": True,
            "chunks": cached.get("chunks", []),
            "source_id": kb.id,
            "source_name": kb.name,
            "file_name": kb.file_url or "",
            "cache_hit": True,
        }

    # P0-1 hybrid retrieval (dense + lexical RRF) — flag-gated, falls back
    # to dense-only on any error.
    if getattr(settings, "RAG_DOC_HYBRID_ENABLED", True):
        try:
            from app.services.document_ingestion.agentic_rag import hybrid_search

            res = hybrid_search(
                org_id=kb.org_id, kb_ids=[kb_id], query_text=query, top_k=top_k
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("hybrid search failed (dense fallback): %s", e)
            res = store.query(
                org_id=kb.org_id, kb_ids=[kb_id], query_text=query, top_k=top_k
            )
    else:
        res = store.query(
            org_id=kb.org_id, kb_ids=[kb_id], query_text=query, top_k=top_k
        )
    cache.store(query, [kb_id], res)
    return {
        "success": True,
        "chunks": res["chunks"],
        "source_id": kb.id,
        "source_name": kb.name,
        "file_name": kb.file_url or "",
    }


async def answer(db: Session, kb_id: str, question: str) -> dict:
    """Vector search + LLM synthesis -> prose answer with citations.

    Agentic-RAG additions (flag-gated, fail-open):
    - query decomposition (P2-5): multi-part questions → parallel sub-searches
    - retrieval grading + one self-correction round (P1-3)
    """
    top_k = 6

    # P2-5 decomposition: multi-part questions retrieved as sub-queries.
    from app.services.document_ingestion.agentic_rag import (
        decompose_query, hybrid_search, settings_mod,
    )

    sub_queries = await decompose_query(question)
    if len(sub_queries) > 1:
        kb = _load_kb(db, kb_id)
        if kb is not None and kb.indexing_status == "ready":
            import asyncio as _asyncio

            merged: dict[str, dict] = {}
            for sub in sub_queries:
                try:
                    res = hybrid_search(
                        org_id=kb.org_id, kb_ids=[kb_id],
                        query_text=sub, top_k=top_k,
                    )
                    for c in res.get("chunks", []):
                        key = (c.get("kb_id"), c.get("chunk_index"),
                               c.get("file_name"))
                        if key not in merged or c.get("score", 0) > merged[key].get("score", 0):
                            merged[key] = c
                except Exception as e:  # noqa: BLE001 — sub-query failure non-fatal
                    logger.debug("sub-query %r failed: %s", sub[:60], e)
            chunks = sorted(merged.values(), key=lambda c: c.get("score", 0),
                            reverse=True)[:top_k]
            sr = {"success": True, "chunks": chunks, "source_id": kb.id,
                  "source_name": kb.name}
        else:
            sr = search(db, kb_id, question, top_k=top_k)
    else:
        sr = search(db, kb_id, question, top_k=top_k)
    if not sr.get("success"):
        return sr
    chunks = sr["chunks"]

    # P1-3 retrieval grading + ONE self-correction round (agentic RAG's
    # self-validation): if the judge says the passages don't answer the
    # question, rewrite the query and re-retrieve once before synthesising.
    if getattr(settings_mod(), "RAG_GRADE_ENABLED", False):
        try:
            from app.services.document_ingestion.agentic_rag import (
                grade_chunks, rewrite_for_retry,
            )

            verdict = await grade_chunks(question, chunks)
            if not verdict.get("sufficient"):
                kb2 = _load_kb(db, kb_id)
                if kb2 is not None and kb2.indexing_status == "ready":
                    retry_q = rewrite_for_retry(question, verdict.get("missing", ""))
                    if retry_q and retry_q.strip() and retry_q != question:
                        logger.info(
                            "answer_from_documents: grade=insufficient — one "
                            "self-correction round (missing=%s)",
                            verdict.get("missing", "")[:120],
                        )
                        res2 = hybrid_search(
                            org_id=kb2.org_id, kb_ids=[kb_id],
                            query_text=retry_q, top_k=top_k,
                        )
                        better = res2.get("chunks") or []
                        if better:
                            chunks = better
        except Exception as e:  # noqa: BLE001 — grading never blocks the answer
            logger.debug("retrieval grading failed (non-fatal): %s", e)
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
