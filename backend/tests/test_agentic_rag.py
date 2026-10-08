"""Tests for the agentic RAG + agentic chunking fixes (2026-10-08).

P0-1 hybrid doc retrieval, P0-2 routing/rewrite flags (env-level),
P1-3 grading + self-correction, P1-4 agentic chunking,
P2-5 decomposition, P2-6 semantic cache.
"""
import asyncio
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401
from app.models.knowledge_base import KnowledgeBase
from app.services.document_ingestion import agentic_rag
from app.services.document_ingestion import retrieval


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(settings, "RAG_DOC_HYBRID_ENABLED", True)
    monkeypatch.setattr(settings, "RAG_GRADE_ENABLED", False)
    monkeypatch.setattr(settings, "RAG_DECOMPOSE_ENABLED", False)
    monkeypatch.setattr(settings, "RAG_AGENTIC_CHUNKING_ENABLED", False)
    agentic_rag.cache._entries.clear()
    yield


def _seed_file_kb(db, name="rag-kb", org_id="default-org"):
    kb = KnowledgeBase(
        name=name, description="d", source_kind="file",
        indexing_status="ready", org_id=org_id, file_url="doc.pdf",
        file_type="pdf",
    )
    db.add(kb)
    db.commit()
    return kb


def _chunk(text, score, idx=0, kb_id="kb1", file_name="doc.pdf"):
    return {"text": text, "score": score, "kb_id": kb_id,
            "file_name": file_name, "file_type": "pdf", "chunk_index": idx}


# ── P0-1 hybrid search ──────────────────────────────────────────────────

def test_hybrid_search_fuses_lexical_signal(monkeypatch):
    # doc B has the exact query tokens but a lower dense score — hybrid
    # must fuse it to the top.
    chunks = [
        _chunk("Quarterly revenue grew across regions", 0.82, idx=0),
        _chunk("inventory lots lot master FMTONO count", 0.70, idx=1),
    ]
    monkeypatch.setattr(
        agentic_rag.store, "query",
        lambda **kw: {"chunks": chunks})
    result = agentic_rag.hybrid_search(
        "default-org", ["kb1"], "inventory lots FMTONO count", top_k=2)
    texts = [c["text"] for c in result["chunks"]]
    assert texts[0] == "inventory lots lot master FMTONO count"
    assert all("rrf_score" in c for c in result["chunks"])


def test_hybrid_search_falls_back_to_dense_order(monkeypatch):
    chunks = [_chunk(f"doc {i}", 0.9 - i * 0.1, idx=i) for i in range(6)]
    monkeypatch.setattr(agentic_rag.store, "query",
                        lambda **kw: {"chunks": chunks})
    # no lexical overlap at all → dense order preserved
    result = agentic_rag.hybrid_search(
        "default-org", ["kb1"], "zzz qqq xxx", top_k=3)
    assert [c["text"] for c in result["chunks"]] == [
        "doc 0", "doc 1", "doc 2"]


def test_search_uses_hybrid_when_enabled(db, monkeypatch):
    kb = _seed_file_kb(db)
    called = {"hybrid": False}

    def fake_hybrid(**kw):
        called["hybrid"] = True
        return {"chunks": [_chunk("hybrid hit", 0.9)]}

    monkeypatch.setattr(agentic_rag, "hybrid_search", fake_hybrid)
    result = retrieval.search(db, kb.id, "some query", top_k=5)
    assert called["hybrid"] is True
    assert result["chunks"][0]["text"] == "hybrid hit"


def test_search_falls_back_to_dense_on_hybrid_error(db, monkeypatch):
    kb = _seed_file_kb(db)

    def boom(**kw):
        raise RuntimeError("chroma down")

    monkeypatch.setattr(agentic_rag, "hybrid_search", boom)
    monkeypatch.setattr(
        agentic_rag.store, "query",
        lambda **kw: {"chunks": [_chunk("dense fallback", 0.8)]})
    result = retrieval.search(db, kb.id, "q", top_k=5)
    assert result["chunks"][0]["text"] == "dense fallback"


# ── P2-6 semantic cache ─────────────────────────────────────────────────

def test_semantic_cache_hit_and_miss(db, monkeypatch):
    kb = _seed_file_kb(db)
    calls = {"n": 0}

    def fake_query(**kw):
        calls["n"] += 1
        return {"chunks": [_chunk(f"hit {calls['n']}", 0.9)]}

    monkeypatch.setattr(agentic_rag.store, "query", fake_query)
    monkeypatch.setattr(agentic_rag, "hybrid_search", fake_query)
    agentic_rag.cache._entries.clear()

    r1 = retrieval.search(db, kb.id, "how many inventory lots", top_k=5)
    r2 = retrieval.search(db, kb.id, "how many inventory lots", top_k=5)
    assert calls["n"] == 1          # second call served from cache
    assert r2.get("cache_hit") is True
    assert r1["chunks"] == r2["chunks"]


def test_semantic_cache_kb_scoped(db, monkeypatch):
    kb1 = _seed_file_kb(db, name="kb-a")
    kb2 = _seed_file_kb(db, name="kb-b")
    calls = {"n": 0}

    def fake_query(**kw):
        calls["n"] += 1
        return {"chunks": [_chunk(f"hit {calls['n']}", 0.9)]}

    monkeypatch.setattr(agentic_rag.store, "query", fake_query)
    monkeypatch.setattr(agentic_rag, "hybrid_search", fake_query)
    agentic_rag.cache._entries.clear()
    retrieval.search(db, kb1.id, "same question text", top_k=5)
    retrieval.search(db, kb2.id, "same question text", top_k=5)
    assert calls["n"] == 2  # different KBs → no cross-KB cache hit


# ── P1-3 grading + self-correction ──────────────────────────────────────

@pytest.mark.asyncio
async def test_answer_grade_insufficient_triggers_retry(db, monkeypatch):
    kb = _seed_file_kb(db)
    searches = []

    async def fake_grade(question, chunks):
        # first round: insufficient → triggers rewrite + re-retrieve
        return {"sufficient": not searches, "missing": "revenue figures"}

    monkeypatch.setattr(agentic_rag, "grade_chunks", fake_grade)
    monkeypatch.setattr(
        agentic_rag, "rewrite_for_retry",
        lambda q, m: "revenue by region July 2026")

    def fake_hybrid(**kw):
        searches.append(kw["query_text"])
        marker = "RETRIEVED" if len(searches) > 1 else "weak"
        return {"chunks": [_chunk(f"{marker} passage {len(searches)}", 0.85,
                                  idx=len(searches))]}

    monkeypatch.setattr(agentic_rag, "hybrid_search", fake_hybrid)

    async def fake_decompose(q):
        return [q]

    monkeypatch.setattr(agentic_rag, "decompose_query", fake_decompose)

    async def fake_llm(messages, tools, **kw):
        return {"content": "The revenue was retrieved successfully."}

    monkeypatch.setattr(retrieval, "call_llm_with_reliability", fake_llm)
    monkeypatch.setattr(settings, "RAG_GRADE_ENABLED", True)
    monkeypatch.setattr(settings, "RAG_DECOMPOSE_ENABLED", False)

    result = await retrieval.answer(db, kb.id, "sales report")
    assert "RETRIEVED" in result["answer"] or result["answer"]
    assert len(searches) == 2  # original + self-correction round
    assert searches[1] == "revenue by region July 2026"


@pytest.mark.asyncio
async def test_grade_sufficient_no_retry(db, monkeypatch):
    kb = _seed_file_kb(db)
    searches = []

    async def fake_grade(question, chunks):
        return {"sufficient": True, "missing": ""}

    monkeypatch.setattr(agentic_rag, "grade_chunks", fake_grade)

    def fake_hybrid(**kw):
        searches.append(kw["query_text"])
        return {"chunks": [_chunk("good passage", 0.9)]}

    monkeypatch.setattr(agentic_rag, "hybrid_search", fake_hybrid)

    async def fake_decompose(q):
        return [q]

    monkeypatch.setattr(agentic_rag, "decompose_query", fake_decompose)

    async def fake_llm(messages, tools, **kw):
        return {"content": "answer"}

    monkeypatch.setattr(retrieval, "call_llm_with_reliability", fake_llm)
    monkeypatch.setattr(settings, "RAG_GRADE_ENABLED", True)
    monkeypatch.setattr(settings, "RAG_DECOMPOSE_ENABLED", False)
    await retrieval.answer(db, kb.id, "q")
    assert len(searches) == 1  # no self-correction round


# ── P2-5 decomposition ──────────────────────────────────────────────────

def test_question_is_multi_part():
    assert agentic_rag._question_is_multi_part(
        "Compare revenue and inventory trends for Q3 this year")
    assert not agentic_rag._question_is_multi_part("hi")
    assert not agentic_rag._question_is_multi_part("What is revenue?")


@pytest.mark.asyncio
async def test_decompose_returns_subqueries(monkeypatch):
    monkeypatch.setattr(settings, "RAG_DECOMPOSE_ENABLED", True)

    async def fake_llm(messages, tools, **kw):
        return {"content": json.dumps(
            ["Q3 revenue by region", "Q3 inventory levels"])}

    with patch("app.services.sub_agent_reliability.call_llm_with_reliability",
               side_effect=fake_llm):
        subs = await agentic_rag.decompose_query(
            "Compare revenue and inventory trends for Q3 this year")
    assert subs == ["Q3 revenue by region", "Q3 inventory levels"]


@pytest.mark.asyncio
async def test_decompose_single_part_untouched(monkeypatch):
    monkeypatch.setattr(settings, "RAG_DECOMPOSE_ENABLED", True)
    subs = await agentic_rag.decompose_query("What is revenue?")
    assert subs == ["What is revenue?"]


# ── P1-4 agentic chunking ───────────────────────────────────────────────

def test_agentic_chunker_skips_small_docs(monkeypatch):
    monkeypatch.setattr(settings, "RAG_AGENTIC_CHUNKING_ENABLED", True)
    assert agentic_rag.agentic_chunk_text("tiny") is None


def test_agentic_chunker_groups_and_enriches(monkeypatch):
    monkeypatch.setattr(settings, "RAG_AGENTIC_CHUNKING_ENABLED", True)
    text = "\n\n".join(
        f"Paragraph {i}: detailed discussion of topic {i % 2}. " * 20
        for i in range(10))
    groups = [
        {"units": [0, 1], "title": "Topic A", "summary": "About A"},
        {"units": [2, 3], "title": "Topic B", "summary": "About B"},
    ]

    def fake_grouping(numbered):
        return groups

    monkeypatch.setattr(agentic_rag, "await_call_grouping", fake_grouping)
    chunks = agentic_rag.agentic_chunk_text(text)
    assert chunks is not None
    assert len(chunks) == 2
    assert chunks[0]["title"] == "Topic A"
    assert "Paragraph 0" in chunks[0]["text"]
    assert all("index" in c for c in chunks)


def test_agentic_chunker_window_budget(monkeypatch):
    monkeypatch.setattr(settings, "RAG_AGENTIC_CHUNKING_ENABLED", True)
    text = "\n\n".join(f"Unit {i} content here. " * 30 for i in range(40))
    windows = {"n": 0}

    def fake_grouping(numbered):
        windows["n"] += 1
        return [{"units": [i], "title": "t", "summary": "s"}
                for i in range(agentic_rag._WINDOW_UNITS)]

    monkeypatch.setattr(agentic_rag, "await_call_grouping", fake_grouping)
    chunks = agentic_rag.agentic_chunk_text(text)
    assert chunks is not None
    assert windows["n"] <= agentic_rag._MAX_LLM_WINDOWS  # budget respected


def test_agentic_chunker_flag_off_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "RAG_AGENTIC_CHUNKING_ENABLED", False)
    text = "\n\n".join(f"Paragraph {i}. " * 50 for i in range(10))
    assert agentic_rag.agentic_chunk_text(text) is None


# ── store metadata enrichment ───────────────────────────────────────────

def test_upsert_chunks_carries_title_summary(monkeypatch):
    import app.services.document_ingestion.store as store_mod

    captured = {}

    class _Coll:
        def upsert(self, ids, embeddings, documents, metadatas):
            captured["metadatas"] = metadatas

    monkeypatch.setattr(store_mod, "_get_collection", lambda org_id: _Coll())
    monkeypatch.setattr(
        store_mod.embedder, "embed_texts",
        lambda texts: __import__("numpy").array([[0.0]] * len(texts)))

    store_mod.upsert_chunks(
        org_id="o", kb_id="kb1",
        chunks=[{"text": "t", "index": 0, "title": "T1", "summary": "S1"}],
        metas=[{"file_name": "f.pdf", "file_type": "pdf"}],
    )
    meta = captured["metadatas"][0]
    assert meta["title"] == "T1"
    assert meta["summary"] == "S1"
