"""Tests for the upgraded document_ingestion.store module.

The store upgrade adds:
- ``CollectionStore`` class — operates on a single named collection
  (one of the 9 EDIA semantic collections, e.g. ``industry_reports``,
  ``weekly_reports``, etc.). Wraps ChromaDB operations with metadata
  safety, a configurable embedding function, and clean test isolation.
- ``MultiCollectionStore`` — owns a per-org registry of CollectionStore
  instances keyed by collection name. Provides bulk upsert/query across
  multiple collections in a single call.
- ``hybrid_query`` — performs dense (vector) + lexical (BM25-like) search
  on a single collection, fused with Reciprocal Rank Fusion (RRF, k=60).
- ``embed_dual`` helper — returns both dense embeddings and lexical
  tokens for a given text in one call.
- Backward-compatible module-level functions: ``upsert_chunks``,
  ``query``, ``delete_kb``, ``count``, ``reset_for_tests`` (unchanged
  signatures) — these still work for the existing KB-per-org path.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.document_ingestion import store
from app.services.document_ingestion.store import (
    CollectionStore,
    MultiCollectionStore,
    hybrid_query,
    rrf_fuse,
    tokenize_lexical,
    upsert_chunks,
    query,
    delete_kb,
    count,
    reset_for_tests,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_chroma_dir(monkeypatch):
    """Redirect CHROMA_DIR to a per-test temp directory."""
    d = tempfile.mkdtemp(prefix="chroma_test_")
    monkeypatch.setenv("CHROMA_DIR", d)
    store.reset_for_tests()
    yield d
    shutil.rmtree(d, ignore_errors=True)
    store.reset_for_tests()


class MockEmbeddingFn:
    """Minimal ChromaDB-compatible embedding function for tests."""

    DIM = 16

    def name(self) -> str:
        return "mock-embedding-fn-v1"

    def __call__(self, input):
        return self.embed_documents(input)

    def embed_documents(self, texts):
        return [self._embed_one(t) for t in texts]

    def embed_query(self, input=None, text=None):
        q = input if input is not None else text
        if isinstance(q, list):
            q = q[0] if q else ""
        return [self._embed_one(str(q) if q else "")]

    @staticmethod
    def _embed_one(text):
        if not text:
            return [0.0] * 16
        vec = [0.0] * 16
        for i, ch in enumerate(text[:64]):
            vec[(ord(ch) + i) % 16] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]


@pytest.fixture
def embed_fn():
    return MockEmbeddingFn()


# ---------------------------------------------------------------------------
# Lexical tokenization
# ---------------------------------------------------------------------------


class TestTokenizeLexical:
    def test_ascii_tokens(self):
        toks = tokenize_lexical("DCPD price increased")
        assert "dcpd" in toks
        assert "price" in toks
        assert "increased" in toks

    def test_cjk_bigrams(self):
        toks = tokenize_lexical("乙烯价格")
        # Should produce bigrams for CJK chars
        assert any("乙烯" in t for t in toks)
        assert any("烯价" in t for t in toks)

    def test_mixed_text(self):
        toks = tokenize_lexical("DCPD 乙烯价格 2024")
        assert "dcpd" in toks
        assert any("乙烯" in t for t in toks)
        assert any("2024" in t for t in toks)

    def test_empty_text(self):
        assert tokenize_lexical("") == []

    def test_lowercased_ascii(self):
        toks = tokenize_lexical("PRICE Increase")
        assert "price" in toks
        assert "increase" in toks


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------


class TestRRFFuse:
    def test_rrf_fuse_empty(self):
        assert rrf_fuse([], []) == []

    def test_rrf_fuse_one_list(self):
        # Only dense results → returns dense with RRF weights
        dense = [{"id": "a"}, {"id": "b"}]
        out = rrf_fuse(dense, [])
        assert len(out) == 2
        assert out[0]["id"] == "a"
        # RRF weight: 1/(60+1) for first
        assert abs(out[0]["score"] - 1 / 61) < 1e-6

    def test_rrf_fuse_two_lists_overlap(self):
        dense = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        sparse = [{"id": "b"}, {"id": "a"}, {"id": "d"}]
        out = rrf_fuse(dense, sparse, k=60)
        # 'a' is in both → top score
        assert out[0]["id"] == "a"
        # 'b' is in both → second
        assert out[1]["id"] == "b"
        # All 4 unique ids present
        assert {x["id"] for x in out} == {"a", "b", "c", "d"}

    def test_rrf_fuse_k_parameter(self):
        dense = [{"id": "a"}]
        sparse = [{"id": "a"}]
        out = rrf_fuse(dense, sparse, k=10)
        # 'a' is rank 1 in both → score = 1/(10+1) + 1/(10+1) = 2/11
        assert abs(out[0]["score"] - 2 / 11) < 1e-6

    def test_rrf_fuse_preserves_metadata(self):
        dense = [{"id": "a", "text": "alpha"}]
        sparse = []
        out = rrf_fuse(dense, [])
        assert out[0]["text"] == "alpha"


# ---------------------------------------------------------------------------
# CollectionStore
# ---------------------------------------------------------------------------


class TestCollectionStore:
    def test_collection_store_init(self, tmp_chroma_dir, embed_fn):
        cs = CollectionStore(
            org_id="org1",
            collection_name="industry_reports",
            embedding_fn=embed_fn,
        )
        assert cs.org_id == "org1"
        assert cs.collection_name == "industry_reports"
        assert cs.embedding_fn is embed_fn

    def test_collection_store_upsert_and_get(self, tmp_chroma_dir, embed_fn):
        cs = CollectionStore(
            org_id="org1",
            collection_name="industry_reports",
            embedding_fn=embed_fn,
        )
        cs.upsert(
            doc_id="doc1",
            text="乙烯价格持续上涨",
            metadata={"file_name": "report.pdf", "page": 1},
        )
        assert cs.count() == 1

    def test_collection_store_query_returns_results(
        self, tmp_chroma_dir, embed_fn
    ):
        cs = CollectionStore(
            org_id="org1",
            collection_name="industry_reports",
            embedding_fn=embed_fn,
        )
        cs.upsert(doc_id="doc1", text="DCPD price increased")
        cs.upsert(doc_id="doc2", text="市场波动明显")
        results = cs.query("DCPD price", top_k=2)
        assert len(results) > 0
        assert all("text" in r for r in results)
        assert all("score" in r for r in results)

    def test_collection_store_delete(self, tmp_chroma_dir, embed_fn):
        cs = CollectionStore(
            org_id="org1",
            collection_name="industry_reports",
            embedding_fn=embed_fn,
        )
        cs.upsert(doc_id="doc1", text="text 1")
        cs.upsert(doc_id="doc2", text="text 2")
        assert cs.count() == 2
        cs.delete(doc_id="doc1")
        assert cs.count() == 1

    def test_collection_store_empty_metadata_safety(
        self, tmp_chroma_dir, embed_fn
    ):
        # ChromaDB 1.5+ requires non-empty metadata — store must add a placeholder
        cs = CollectionStore(
            org_id="org1",
            collection_name="industry_reports",
            embedding_fn=embed_fn,
        )
        # Should not raise
        cs.upsert(doc_id="doc1", text="some text", metadata={})
        cs.upsert(doc_id="doc2", text="other text", metadata=None)
        assert cs.count() == 2

    def test_collection_store_isolation_by_org(
        self, tmp_chroma_dir, embed_fn
    ):
        cs1 = CollectionStore(
            org_id="org1",
            collection_name="industry_reports",
            embedding_fn=embed_fn,
        )
        cs2 = CollectionStore(
            org_id="org2",
            collection_name="industry_reports",
            embedding_fn=embed_fn,
        )
        cs1.upsert(doc_id="d1", text="org1 only")
        cs2.upsert(doc_id="d1", text="org2 only")
        assert cs1.count() == 1
        assert cs2.count() == 1
        # Results are scoped
        r1 = cs1.query("org1", top_k=5)
        r2 = cs2.query("org2", top_k=5)
        assert any("org1" in r["text"] for r in r1)
        assert any("org2" in r["text"] for r in r2)


# ---------------------------------------------------------------------------
# MultiCollectionStore
# ---------------------------------------------------------------------------


class TestMultiCollectionStore:
    def test_get_or_create(self, tmp_chroma_dir, embed_fn):
        mcs = MultiCollectionStore(org_id="org1", embedding_fn=embed_fn)
        cs = mcs.get_or_create("industry_reports")
        assert isinstance(cs, CollectionStore)
        assert cs.collection_name == "industry_reports"
        # Returns the same instance on second call
        cs2 = mcs.get_or_create("industry_reports")
        assert cs is cs2

    def test_known_collections(self, tmp_chroma_dir, embed_fn):
        from app.services.rag.collection_names import (
            INDUSTRY_REPORTS,
            WEEKLY_REPORTS,
        )
        mcs = MultiCollectionStore(org_id="org1", embedding_fn=embed_fn)
        mcs.get_or_create(INDUSTRY_REPORTS)
        mcs.get_or_create(WEEKLY_REPORTS)
        assert INDUSTRY_REPORTS in mcs.collections
        assert WEEKLY_REPORTS in mcs.collections


# ---------------------------------------------------------------------------
# hybrid_query
# ---------------------------------------------------------------------------


class TestHybridQuery:
    def test_hybrid_query_returns_results(self, tmp_chroma_dir, embed_fn):
        cs = CollectionStore(
            org_id="org1",
            collection_name="industry_reports",
            embedding_fn=embed_fn,
        )
        cs.upsert(doc_id="d1", text="DCPD price increased significantly")
        cs.upsert(doc_id="d2", text="Market volatility in ethylene sector")
        cs.upsert(doc_id="d3", text="Crude oil supply trends")
        out = hybrid_query(cs, "DCPD price", top_k=3)
        assert len(out) > 0
        # Dense match for "DCPD" should rank first
        assert out[0]["id"] in ("d1", "d2")

    def test_hybrid_query_boosts_keyword_match(self, tmp_chroma_dir, embed_fn):
        cs = CollectionStore(
            org_id="org1",
            collection_name="industry_reports",
            embedding_fn=embed_fn,
        )
        cs.upsert(doc_id="d1", text="Short blurb.")
        cs.upsert(doc_id="d2", text="DCPD price DCPD price DCPD")
        out = hybrid_query(cs, "DCPD price", top_k=2)
        # d2 has many "DCPD" tokens → should rank first via lexical boost
        assert out[0]["id"] == "d2"

    def test_hybrid_query_with_metadata_filter(
        self, tmp_chroma_dir, embed_fn
    ):
        cs = CollectionStore(
            org_id="org1",
            collection_name="industry_reports",
            embedding_fn=embed_fn,
        )
        cs.upsert(doc_id="d1", text="alpha bravo", metadata={"tag": "x"})
        cs.upsert(doc_id="d2", text="alpha charlie", metadata={"tag": "y"})
        out = hybrid_query(cs, "alpha", top_k=5, where={"tag": "x"})
        # Only d1 has tag=x
        assert len(out) >= 1
        assert all(r["metadata"].get("tag") == "x" for r in out)

    def test_hybrid_query_empty_collection(self, tmp_chroma_dir, embed_fn):
        cs = CollectionStore(
            org_id="org1",
            collection_name="industry_reports",
            embedding_fn=embed_fn,
        )
        out = hybrid_query(cs, "anything", top_k=5)
        assert out == []


# ---------------------------------------------------------------------------
# Backward-compatible module-level API
# ---------------------------------------------------------------------------


class TestBackwardCompatAPI:
    def test_upsert_chunks_still_works(self, tmp_chroma_dir, embed_fn, monkeypatch):
        # Patch the module-level embedder to use the mock
        monkeypatch.setattr(
            "app.services.document_ingestion.embedder.embed_texts",
            lambda texts: __import__("numpy").array(
                [MockEmbeddingFn._embed_one(t) for t in texts], dtype="float32"
            ),
        )
        chunks = [{"text": "hello", "index": 0}, {"text": "world", "index": 1}]
        metas = [{"file_name": "a.txt"}, {"file_name": "a.txt"}]
        n = upsert_chunks("org1", "kb1", chunks, metas)
        assert n == 2

    def test_query_returns_chunks_dict(self, tmp_chroma_dir, embed_fn, monkeypatch):
        monkeypatch.setattr(
            "app.services.document_ingestion.embedder.embed_texts",
            lambda texts: __import__("numpy").array(
                [MockEmbeddingFn._embed_one(t) for t in texts], dtype="float32"
            ),
        )
        monkeypatch.setattr(
            "app.services.document_ingestion.embedder.embed_query",
            lambda text: __import__("numpy").array(
                MockEmbeddingFn._embed_one(text), dtype="float32"
            ),
        )
        chunks = [{"text": "alpha beta", "index": 0}]
        metas = [{"file_name": "f.txt"}]
        upsert_chunks("org1", "kb1", chunks, metas)
        out = query("org1", ["kb1"], "alpha", top_k=3)
        assert "chunks" in out
        assert isinstance(out["chunks"], list)

    def test_delete_kb(self, tmp_chroma_dir, embed_fn, monkeypatch):
        monkeypatch.setattr(
            "app.services.document_ingestion.embedder.embed_texts",
            lambda texts: __import__("numpy").array(
                [MockEmbeddingFn._embed_one(t) for t in texts], dtype="float32"
            ),
        )
        chunks = [{"text": "x", "index": 0}]
        metas = [{}]
        upsert_chunks("org1", "kb1", chunks, metas)
        assert count("org1", "kb1") >= 1
        delete_kb("org1", "kb1")
        assert count("org1", "kb1") == 0

    def test_reset_for_tests(self):
        # Should not raise
        reset_for_tests()
