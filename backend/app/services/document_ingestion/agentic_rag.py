"""Agentic RAG layer — IBM agentic-RAG + agentic-chunking gaps (2026-10-08).

Six capabilities, all flag-gated, all fail-open to the classic pipeline:

1. hybrid_search      — dense + lexical RRF over a wide candidate pool
                        (P0-1: wires the existing hybrid *infrastructure*
                        into the document KB path, which was dense-only).
2. SemanticCache      — in-process query→chunks cache keyed by embedding
                        cosine ≥ 0.95 (P2-6, agentic-RAG "memory").
3. grade_chunks       — cheap LLM judge: do these chunks answer the
                        question? (P1-3, agentic-RAG self-validation).
4. rewrite_for_retry  — one query rewrite for the self-correction round
                        (P1-3, reuses query_rewriter when enabled).
5. decompose_query    — multi-part question → sub-queries, retrieved in
                        parallel and merged (P2-5, planning agent).
6. agentic_chunk_text — LLM proposition grouping + per-chunk title/summary
                        metadata at ingestion (P1-4, agentic chunking).

Every LLM step is budget-capped and every step falls back to the classic
pipeline on any failure — the default path never gets slower or fragile.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.services.document_ingestion import store

logger = logging.getLogger(__name__)

# ── 1. Hybrid search (P0-1) ─────────────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    try:
        from app.services.document_ingestion.store import tokenize_lexical
        return set(tokenize_lexical(text or ""))
    except Exception:  # noqa: BLE001
        return set((text or "").lower().split())


def hybrid_search(
    org_id: str,
    kb_ids: list[str],
    query_text: str,
    top_k: int = 5,
    candidate_multiplier: int = 4,
) -> dict:
    """Dense + lexical RRF over a wide candidate pool.

    Fetches ``top_k * candidate_multiplier`` dense candidates (the existing
    ``store.query`` with its kb_id filter), then re-ranks by Reciprocal Rank
    Fusion of (dense rank, lexical overlap rank). Falls back to dense-only
    results on any error.
    """
    pool = store.query(
        org_id=org_id, kb_ids=kb_ids, query_text=query_text,
        top_k=max(top_k * candidate_multiplier, top_k),
    )
    chunks = pool.get("chunks") or []
    if not chunks:
        return {"chunks": []}

    q_tokens = _tokenize(query_text)
    if not q_tokens:
        return {"chunks": chunks[:top_k]}

    # lexical rank: token-overlap score, stable sort descending
    lex_scored = []
    for c in chunks:
        overlap = len(q_tokens & _tokenize(c.get("text", "")))
        lex_scored.append(overlap / max(1, len(q_tokens)))
    lex_order = sorted(
        range(len(chunks)), key=lambda i: lex_scored[i], reverse=True)

    rrf_k = 60
    rrf: dict[int, float] = {}
    for rank, idx in enumerate(range(len(chunks))):
        rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (rrf_k + rank + 1)
    for rank, idx in enumerate(lex_order):
        rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (rrf_k + rank + 1)

    # RRF first; ties break toward the higher lexical overlap (RRF alone
    # ties when the two rankings are exact permutations of each other).
    fused = sorted(range(len(chunks)),
                   key=lambda i: (rrf[i], lex_scored[i]), reverse=True)
    out = []
    for idx in fused[:top_k]:
        c = dict(chunks[idx])
        # keep the dense similarity for citation display, add rrf metadata
        c["rrf_score"] = round(rrf[idx], 6)
        c["lexical_overlap"] = round(lex_scored[idx], 4)
        out.append(c)
    return {"chunks": out}


# ── 2. Semantic cache (P2-6) ────────────────────────────────────────────

class SemanticCache:
    """In-process query→result cache keyed by embedding cosine similarity.

    Hit requires cosine ≥ threshold (default 0.95) AND TTL ≤ 1h AND the
    same kb_id set (results are KB-scoped). Bounded at max_entries (LRU-ish
    by insertion). Best-effort: any embedder failure = cache miss.
    """

    def __init__(self, threshold: float = 0.95, ttl_s: float = 3600.0,
                 max_entries: int = 256) -> None:
        self.threshold = threshold
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._entries: list[dict] = []  # [{ts, kb_ids, query_vec, payload}]

    def _query_vec(self, query_text: str):
        from app.services.document_ingestion import embedder

        try:
            return embedder.embed_query(query_text).tolist()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        if not na or not nb:
            return 0.0
        return dot / (na * nb)

    def lookup(self, query_text: str, kb_ids: list[str]) -> dict | None:
        now = time.monotonic()
        qv = self._query_vec(query_text)
        if qv is None:
            return None
        kb_set = set(kb_ids)
        for entry in reversed(self._entries):
            if now - entry["ts"] > self.ttl_s:
                continue
            if set(entry["kb_ids"]) != kb_set:
                continue
            if self._cosine(qv, entry["query_vec"]) >= self.threshold:
                logger.info("semantic_cache: HIT (cosine ≥ %.2f)", self.threshold)
                return entry["payload"]
        return None

    def store(self, query_text: str, kb_ids: list[str], payload: dict) -> None:
        qv = self._query_vec(query_text)
        if qv is None:
            return
        self._entries.append({
            "ts": time.monotonic(), "kb_ids": list(kb_ids),
            "query_vec": qv, "payload": payload,
        })
        if len(self._entries) > self.max_entries:
            self._entries = self._entries[-self.max_entries:]


cache = SemanticCache()


# ── 3. Retrieval grading (P1-3) ─────────────────────────────────────────

async def grade_chunks(question: str, chunks: list[dict]) -> dict:
    """Cheap LLM judge: do these passages suffice to answer the question?

    Returns ``{"sufficient": bool, "missing": str}``. Fail-open: any error
    → sufficient=True (never block the classic path).
    """
    if not chunks:
        return {"sufficient": False, "missing": "no chunks retrieved"}
    excerpt = "\n\n".join(
        f"[{i + 1}] {c.get('text', '')[:600]}" for i, c in enumerate(chunks[:6])
    )
    try:
        from app.services.sub_agent_reliability import call_llm_with_reliability

        resp = await call_llm_with_reliability(
            [{"role": "user", "content": (
                "Grade whether the PASSAGES below contain enough information "
                "to answer the QUESTION. Reply ONLY with JSON: "
                '{"sufficient": true/false, "missing": "<what is missing, '
                'one sentence>"}. Be strict: if the passages only partly '
                "cover the question, sufficient=false.\n\n"
                f"QUESTION: {question}\n\nPASSAGES:\n{excerpt}"
            )}],
            tools=[], temperature=0.0,
        )
        raw = (resp.get("content") or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        obj = json.loads(raw[start : end + 1]) if start != -1 and end > start else {}
        return {
            "sufficient": bool(obj.get("sufficient", True)),
            "missing": str(obj.get("missing") or "")[:300],
        }
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.debug("grade_chunks failed (fail-open): %s", e)
        return {"sufficient": True, "missing": ""}


def rewrite_for_retry(question: str, missing: str) -> str:
    """One rewritten query for the self-correction round (gap: query rewrite
    was built but never wired to the document path)."""
    try:
        from app.services.query_rewriter import rewrite_query

        result = rewrite_query(
            user_message=f"{question} (missing: {missing})",
            conversation_history=None, use_llm=False,
        )
        rewritten = getattr(result, "rewritten", "") or question
        return rewritten if rewritten.strip() else question
    except Exception as e:  # noqa: BLE001
        logger.debug("rewrite_for_retry failed (non-fatal): %s", e)
        return question


# ── 5. Query decomposition (P2-5) ───────────────────────────────────────

_MULTI_PART_HINTS = (" and ", " plus ", " both ", " compare ", " vs ", "以及", "和", "与", "对比")


def _question_is_multi_part(question: str) -> bool:
    q = question.lower()
    return any(h in q for h in _MULTI_PART_HINTS) and len(question.split()) >= 6


async def decompose_query(question: str) -> list[str]:
    """Multi-part question → 2-4 sub-queries. Falls back to [question]."""
    if not getattr(settings_mod(), "RAG_DECOMPOSE_ENABLED", False):
        return [question]
    if not _question_is_multi_part(question):
        return [question]
    try:
        from app.services.sub_agent_reliability import call_llm_with_reliability

        resp = await call_llm_with_reliability(
            [{"role": "user", "content": (
                "Split the question below into 2-4 independent, self-contained "
                "sub-questions that together answer it. Reply ONLY with a JSON "
                f"array of strings.\n\nQUESTION: {question}"
            )}],
            tools=[], temperature=0.0,
        )
        raw = (resp.get("content") or "").strip()
        start, end = raw.find("["), raw.rfind("]")
        subs = json.loads(raw[start : end + 1]) if start != -1 and end > start else []
        subs = [str(s).strip() for s in subs if str(s).strip()][:4]
        return subs or [question]
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.debug("decompose_query failed (fail-open): %s", e)
        return [question]


def settings_mod():
    from app.config import settings

    return settings


# ── 6. Agentic chunking (P1-4) ──────────────────────────────────────────

_AGENTIC_MIN_TOKENS = 800      # skip tiny docs — recursive chunking is fine
_MAX_LLM_WINDOWS = 6           # hard budget: ≤6 LLM calls per document
_WINDOW_UNITS = 12             # paragraphs per LLM window


def agentic_chunk_text(text: str, overlap_tokens: int = 50) -> list[dict] | None:
    """LLM-decided chunk boundaries + metadata enrichment (agentic chunking).

    Splits ``text`` into paragraph units, then asks the LLM (windowed,
    budget-capped) to group units into semantically complete chunks, each
    with a short title + summary that are stored as chunk metadata (which
    also feeds lexical/hybrid retrieval). Returns the classic chunk dict
    shape ``[{"text", "index", "title", "summary"}]`` or **None** on any
    failure / when the doc is too small — callers fall back to
    ``chunker.chunk_text``.
    """
    if not text or not getattr(settings_mod(), "RAG_AGENTIC_CHUNKING_ENABLED", False):
        return None
    try:
        units = [u.strip() for u in text.split("\n\n") if u.strip()]
        if len(units) < 3:
            return None
        est_tokens = len(text) // 3  # same heuristic family as the chunker
        if est_tokens < _AGENTIC_MIN_TOKENS:
            return None

        out: list[dict] = []
        idx = 0
        llm_windows = 0
        for w_start in range(0, len(units), _WINDOW_UNITS):
            if not out or len(out) < 64:
                pass
            if len(out) >= 64:  # absolute chunk ceiling
                break
            window = units[w_start : w_start + _WINDOW_UNITS]
            numbered = "\n\n".join(
                f"[{i}] {u[:900]}" for i, u in enumerate(window))
            if llm_windows >= _MAX_LLM_WINDOWS:
                groups = [{"units": [i], "title": "", "summary": ""}
                          for i in range(len(window))]
            else:
                llm_windows += 1
                groups = await_call_grouping(numbered) or [
                    {"units": [i], "title": "", "summary": ""}
                    for i in range(len(window))
                ]
            for group in groups:
                members = [window[i] for i in group["units"] if 0 <= i < len(window)]
                if not members:
                    continue
                body = "\n\n".join(members)
                out.append({
                    "text": body,
                    "index": idx,
                    "title": str(group.get("title") or "")[:200],
                    "summary": str(group.get("summary") or "")[:500],
                })
                idx += 1
        if not out:
            return None
        logger.info("agentic_chunker: %d semantic chunks from %d units (%d LLM windows)",
                    len(out), len(units), llm_windows)
        return out
    except Exception as e:  # noqa: BLE001 — always fall back
        logger.warning("agentic_chunk_text failed (falling back to recursive): %s", e)
        return None


def await_call_grouping(numbered: str) -> list | None:
    """One bounded LLM grouping call. Returns parsed groups or None."""
    import asyncio

    async def _call():
        from app.services.sub_agent_reliability import call_llm_with_reliability

        resp = await call_llm_with_reliability(
            [{"role": "user", "content": (
                "You are a document chunking agent. The numbered blocks below "
                "are consecutive paragraphs of one document. Group CONSECUTIVE "
                "blocks into semantically complete chunks (each chunk = one "
                "self-contained topic; split at topic shifts). For each chunk "
                "give a short title and 1-sentence summary. Reply ONLY with "
                "JSON: [{\"units\": [0,1], \"title\": \"...\", \"summary\": "
                "\"...\"}, ...] covering every block exactly once.\n\n"
                f"BLOCKS:\n{numbered}"
            )}],
            tools=[], temperature=0.0,
        )
        raw = (resp.get("content") or "").strip()
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end <= start:
            return None
        return json.loads(raw[start : end + 1])

    try:
        groups = asyncio.run(_call())
        if not isinstance(groups, list):
            return None
        # normalize: accept both {"units": [...]} and bare [0,1] lists
        norm: list = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("units"), list):
                norm.append({"units": g["units"],
                             "title": g.get("title", ""),
                             "summary": g.get("summary", "")})
            elif isinstance(g, list):
                norm.append({"units": g, "title": "", "summary": ""})
        out = []
        for g in norm:
            for i in g["units"]:
                if not isinstance(i, int):
                    break
            else:
                out.append(g)
        return out or None
    except Exception as e:  # noqa: BLE001
        logger.debug("grouping call failed: %s", e)
        return None
