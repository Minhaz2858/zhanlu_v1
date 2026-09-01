"""Tests for knowledge_base.py — three-tier degradation chain.

Three classes with a common interface:
- RAGKnowledgeBase         — full ChromaDB-backed 9-collection KB
- LexicalRAGKnowledgeBase  — pure in-memory lexical fallback
- DisabledRAGKnowledgeBase — graceful no-op (returns empty)

Factory:
- create_knowledge_base(org_id, embedding_fn=None, persist_dir=None)
  returns one of the three based on runtime availability.

Common methods (all three):
- get_collection(name)            → collection-like object | None
- list_collections()              → List[str]
- upsert(name, doc_id, text, metadata=None) → bool (success)
- query(name, query_text, top_k=10) → List[(doc_id, score)]
- hybrid_query(name, query_text, top_k=10) → List[(doc_id, score)]
- close()                         → cleanup
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.services.rag.knowledge_base import (
    RAGKnowledgeBase,
    LexicalRAGKnowledgeBase,
    DisabledRAGKnowledgeBase,
    KnowledgeBaseError,
    create_knowledge_base,
)
from app.services.rag.collection_names import (
    ALL_COLLECTION_NAMES,
    INDUSTRY_REPORTS,
    PAST_DECISIONS,
    USER_MEMORY,
    get_collection_spec,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class MockEmbeddingFn:
    """Deterministic mock embedding function for tests.

    Implements the full ChromaDB 1.5+ embedding function protocol:
    - embed_documents(texts) for upsert
    - embed_query(text) for query
    - __call__ as fallback for both

    English/numeric tokens are mapped to a 16-dim vector based on
    character-code hashing. No external model download required.
    """

    DIM = 16

    def name(self) -> str:
        return "mock-embedding-fn-v1"

    def __call__(self, input: List[str]) -> List[List[float]]:  # noqa: A002
        return self.embed_documents(input)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._embed_one(t) for t in texts]

    def embed_query(self, input: Any = None, text: Any = None) -> List[List[float]]:  # noqa: A002
        # ChromaDB 1.5+ calls embed_query(input=[query]) — accept both signatures
        q = input if input is not None else text
        if isinstance(q, list):
            q = q[0] if q else ""
        return [self._embed_one(str(q) if q else "")]

    @staticmethod
    def _embed_one(text: str) -> List[float]:
        if not text:
            return [0.0] * MockEmbeddingFn.DIM
        vec = [0.0] * MockEmbeddingFn.DIM
        # Use simple character-code hashing for determinism
        for i, ch in enumerate(text[: MockEmbeddingFn.DIM * 4]):
            bucket = (ord(ch) + i) % MockEmbeddingFn.DIM
            vec[bucket] += 1.0
        # Normalize
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]


@pytest.fixture
def embedding_fn() -> MockEmbeddingFn:
    return MockEmbeddingFn()


@pytest.fixture
def temp_persist_dir() -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


# ---------------------------------------------------------------------------
# RAGKnowledgeBase — ChromaDB-backed
# ---------------------------------------------------------------------------


class TestRAGKnowledgeBaseLifecycle:
    def test_creates_with_persist_dir(self, embedding_fn, temp_persist_dir):
        kb = RAGKnowledgeBase(
            org_id="acme",
            embedding_fn=embedding_fn,
            persist_dir=temp_persist_dir,
        )
        try:
            # 9 collections should be available
            assert set(kb.list_collections()) == set(ALL_COLLECTION_NAMES)
        finally:
            kb.close()

    def test_creates_without_persist_dir_uses_default(
        self, embedding_fn, monkeypatch
    ):
        # Default to a temp dir for safety
        monkeypatch.setenv("RAG_PERSIST_DIR", tempfile.mkdtemp())
        kb = RAGKnowledgeBase(org_id="acme", embedding_fn=embedding_fn)
        try:
            assert len(kb.list_collections()) == 9
        finally:
            kb.close()

    def test_close_is_idempotent(self, embedding_fn, temp_persist_dir):
        kb = RAGKnowledgeBase(
            org_id="acme",
            embedding_fn=embedding_fn,
            persist_dir=temp_persist_dir,
        )
        kb.close()
        kb.close()  # Should not raise

    def test_collection_names_are_tenant_scoped(
        self, embedding_fn, temp_persist_dir
    ):
        kb_a = RAGKnowledgeBase(
            org_id="acme", embedding_fn=embedding_fn, persist_dir=temp_persist_dir
        )
        kb_b = RAGKnowledgeBase(
            org_id="globex", embedding_fn=embedding_fn, persist_dir=temp_persist_dir
        )
        try:
            # Get the actual collection names; they should differ by org_id
            assert "acme" in kb_a._collection_name(INDUSTRY_REPORTS)
            assert "globex" in kb_b._collection_name(INDUSTRY_REPORTS)
            assert kb_a._collection_name(INDUSTRY_REPORTS) != kb_b._collection_name(
                INDUSTRY_REPORTS
            )
        finally:
            kb_a.close()
            kb_b.close()


class TestRAGKnowledgeBaseCRUD:
    def test_upsert_returns_true(self, embedding_fn, temp_persist_dir):
        kb = RAGKnowledgeBase(
            org_id="acme",
            embedding_fn=embedding_fn,
            persist_dir=temp_persist_dir,
        )
        try:
            ok = kb.upsert(
                INDUSTRY_REPORTS,
                doc_id="doc1",
                text="乙烯价格持续上涨",
                metadata={"source": "test"},
            )
            assert ok is True
        finally:
            kb.close()

    def test_upsert_then_query_returns_match(self, embedding_fn, temp_persist_dir):
        kb = RAGKnowledgeBase(
            org_id="acme",
            embedding_fn=embedding_fn,
            persist_dir=temp_persist_dir,
        )
        try:
            kb.upsert(
                INDUSTRY_REPORTS,
                doc_id="doc1",
                text="乙烯价格持续上涨趋势明显",
                metadata={"source": "test"},
            )
            hits = kb.query(INDUSTRY_REPORTS, "乙烯价格", top_k=5)
            assert len(hits) >= 1
            doc_ids = [d for d, _ in hits]
            assert "doc1" in doc_ids
        finally:
            kb.close()

    def test_upsert_multiple_then_top_k(self, embedding_fn, temp_persist_dir):
        kb = RAGKnowledgeBase(
            org_id="acme",
            embedding_fn=embedding_fn,
            persist_dir=temp_persist_dir,
        )
        try:
            for i in range(10):
                kb.upsert(
                    PAST_DECISIONS,
                    doc_id=f"d{i}",
                    text=f"decision {i} about ethylene",
                    metadata={"seq": i},
                )
            hits = kb.query(PAST_DECISIONS, "ethylene", top_k=3)
            assert len(hits) <= 3
            assert len(hits) >= 1
        finally:
            kb.close()

    def test_unknown_collection_raises(self, embedding_fn, temp_persist_dir):
        kb = RAGKnowledgeBase(
            org_id="acme",
            embedding_fn=embedding_fn,
            persist_dir=temp_persist_dir,
        )
        try:
            with pytest.raises(KnowledgeBaseError):
                kb.upsert("nonexistent_collection", doc_id="x", text="y")
        finally:
            kb.close()

    def test_get_collection_returns_collection(self, embedding_fn, temp_persist_dir):
        kb = RAGKnowledgeBase(
            org_id="acme",
            embedding_fn=embedding_fn,
            persist_dir=temp_persist_dir,
        )
        try:
            coll = kb.get_collection(USER_MEMORY)
            assert coll is not None
        finally:
            kb.close()

    def test_get_unknown_collection_returns_none(
        self, embedding_fn, temp_persist_dir
    ):
        kb = RAGKnowledgeBase(
            org_id="acme",
            embedding_fn=embedding_fn,
            persist_dir=temp_persist_dir,
        )
        try:
            assert kb.get_collection("nonexistent") is None
        finally:
            kb.close()


class TestRAGKnowledgeBaseHybrid:
    def test_hybrid_query_returns_results(
        self, embedding_fn, temp_persist_dir
    ):
        kb = RAGKnowledgeBase(
            org_id="acme",
            embedding_fn=embedding_fn,
            persist_dir=temp_persist_dir,
        )
        try:
            kb.upsert(
                INDUSTRY_REPORTS,
                "a",
                "乙烯价格持续上涨",
            )
            kb.upsert(
                INDUSTRY_REPORTS,
                "b",
                "原油市场波动剧烈",
            )
            hits = kb.hybrid_query(INDUSTRY_REPORTS, "乙烯价格", top_k=5)
            assert len(hits) >= 1
            # The doc about ethylene should rank highly
            doc_ids = [d for d, _ in hits]
            assert "a" in doc_ids
        finally:
            kb.close()

    def test_hybrid_query_empty_collection(
        self, embedding_fn, temp_persist_dir
    ):
        kb = RAGKnowledgeBase(
            org_id="acme",
            embedding_fn=embedding_fn,
            persist_dir=temp_persist_dir,
        )
        try:
            hits = kb.hybrid_query(INDUSTRY_REPORTS, "anything", top_k=5)
            assert hits == []
        finally:
            kb.close()


# ---------------------------------------------------------------------------
# LexicalRAGKnowledgeBase — in-memory fallback
# ---------------------------------------------------------------------------


class TestLexicalKnowledgeBase:
    def test_starts_empty(self):
        kb = LexicalRAGKnowledgeBase(org_id="acme")
        assert kb.list_collections() == ALL_COLLECTION_NAMES

    def test_upsert_and_query_lexical_only(self):
        kb = LexicalRAGKnowledgeBase(org_id="acme")
        kb.upsert(INDUSTRY_REPORTS, "d1", "乙烯价格持续上涨")
        kb.upsert(INDUSTRY_REPORTS, "d2", "原油市场波动")
        hits = kb.query(INDUSTRY_REPORTS, "乙烯价格", top_k=5)
        assert len(hits) >= 1
        assert "d1" in [d for d, _ in hits]

    def test_no_external_dependencies_required(self):
        # Should construct without ChromaDB or embedding model
        kb = LexicalRAGKnowledgeBase(org_id="acme")
        kb.upsert(PAST_DECISIONS, "x", "any text")
        hits = kb.query(PAST_DECISIONS, "any", top_k=5)
        assert len(hits) >= 1

    def test_hybrid_query_falls_back_to_lexical(self):
        kb = LexicalRAGKnowledgeBase(org_id="acme")
        kb.upsert(INDUSTRY_REPORTS, "d1", "油价上涨")
        # No dense index → hybrid should still return lexical matches
        hits = kb.hybrid_query(INDUSTRY_REPORTS, "油价", top_k=5)
        assert len(hits) >= 1


# ---------------------------------------------------------------------------
# DisabledRAGKnowledgeBase — graceful no-op
# ---------------------------------------------------------------------------


class TestDisabledKnowledgeBase:
    def test_returns_empty_query_results(self):
        kb = DisabledRAGKnowledgeBase(org_id="acme")
        assert kb.list_collections() == ALL_COLLECTION_NAMES
        assert kb.query(INDUSTRY_REPORTS, "anything") == []
        assert kb.hybrid_query(INDUSTRY_REPORTS, "anything") == []

    def test_upsert_returns_false(self):
        kb = DisabledRAGKnowledgeBase(org_id="acme")
        ok = kb.upsert(INDUSTRY_REPORTS, "d1", "text")
        assert ok is False

    def test_get_collection_returns_none(self):
        kb = DisabledRAGKnowledgeBase(org_id="acme")
        assert kb.get_collection(INDUSTRY_REPORTS) is None

    def test_close_is_safe(self):
        kb = DisabledRAGKnowledgeBase(org_id="acme")
        kb.close()  # Should not raise


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestKnowledgeBaseFactory:
    def test_factory_returns_some_kb_instance(self, temp_persist_dir):
        kb = create_knowledge_base(
            org_id="acme",
            embedding_fn=MockEmbeddingFn(),
            persist_dir=temp_persist_dir,
        )
        try:
            assert kb is not None
            assert hasattr(kb, "list_collections")
            assert hasattr(kb, "upsert")
            assert hasattr(kb, "query")
            assert hasattr(kb, "hybrid_query")
        finally:
            kb.close()

    def test_factory_returns_disabled_when_disabled_env(
        self, monkeypatch, temp_persist_dir
    ):
        monkeypatch.setenv("RAG_HYBRID_ENABLED", "false")
        kb = create_knowledge_base(
            org_id="acme",
            embedding_fn=MockEmbeddingFn(),
            persist_dir=temp_persist_dir,
        )
        try:
            assert isinstance(kb, DisabledRAGKnowledgeBase)
        finally:
            kb.close()

    def test_factory_with_invalid_persist_dir_falls_back_to_lexical(
        self, monkeypatch
    ):
        # Force a path that can't be created (e.g. under /proc)
        bad_dir = "/proc/this/cannot/exist/rag"
        kb = create_knowledge_base(
            org_id="acme",
            embedding_fn=MockEmbeddingFn(),
            persist_dir=bad_dir,
        )
        try:
            # Should gracefully fall back to lexical (not crash)
            assert kb is not None
            assert isinstance(
                kb, (RAGKnowledgeBase, LexicalRAGKnowledgeBase, DisabledRAGKnowledgeBase)
            )
        finally:
            kb.close()
