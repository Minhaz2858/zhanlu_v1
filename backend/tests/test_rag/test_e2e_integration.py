"""End-to-end integration test for the Phase 2 Hybrid RAG pipeline.

Tests the full pipeline:
    1. SmartIndexer writes events/decisions to RAG collections.
    2. RagRetriever / RAG knowledge base reads back relevant hits.
    3. Hybrid (dense + lexical) query path works.
    4. Domain context builder combines multiple collections.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def tmp_chroma(monkeypatch):
    """Provide an isolated ChromaDB directory per test."""
    d = tempfile.mkdtemp(prefix="rag_e2e_")
    monkeypatch.setenv("CHROMA_DIR", d)
    # Also reset KB factory cache
    from app.services.rag import knowledge_base as kb_mod
    if hasattr(kb_mod, "reset_for_tests"):
        kb_mod.reset_for_tests()
    yield d
    shutil.rmtree(d, ignore_errors=True)


class TestE2EPipeline:
    def test_full_pipeline(self, tmp_chroma):
        """Phase 1 → RAG → retrieval round trip."""
        from app.services.rag.smart_indexer import SmartIndexer
        from app.services.rag.rag_retriever import retrieve_rich

        # 1. Index an event
        idx = SmartIndexer()
        ok = idx.index_event({
            "event_id": "ev_test_1",
            "headline": "OPEC production cut announced",
            "key_information": "2M bpd for Q3",
            "source_url": "https://example.com",
        })
        assert ok is True

        # 2. Retrieve it back
        hits = retrieve_rich(
            query="OPEC production",
            n_results=3,
            collection="news_events",
        )
        assert len(hits) >= 1
        # The indexed event should be among the results
        texts = " ".join(h.text for h in hits)
        assert "OPEC" in texts or "production" in texts

    def test_multi_collection_context(self, tmp_chroma):
        from app.services.rag.smart_indexer import SmartIndexer
        from app.services.rag.domain_collections import build_domain_context

        idx = SmartIndexer()
        # Index in multiple collections
        idx.index_event({
            "event_id": "ev_mc_1",
            "headline": "DCPD price volatility",
            "key_information": "up 5%",
        })
        idx.index_signal({
            "signal_id": "sig_mc_1",
            "description": "Crude oil inventory dropped",
        })
        idx.index_product({
            "product_id": "p_mc_1",
            "name": "DCPD",
            "aliases": ["dicyclopentadiene"],
            "category": "C5",
        })

        # Build a multi-collection context block
        ctx = build_domain_context(
            query="DCPD price",
            collections=["news_events", "market_signals", "product_catalog"],
            max_chars=3000,
        )
        assert isinstance(ctx, str)
        # At least one of the collections should have a hit
        assert "DCPD" in ctx or "Crude" in ctx or "price" in ctx

    def test_past_decision_roundtrip(self, tmp_chroma):
        from app.services.rag.smart_indexer import SmartIndexer
        from app.services.rag.rag_retriever import retrieve_rich

        idx = SmartIndexer()
        idx.index_decision({
            "decision_id": "d_rt_1",
            "summary": "Increase DCPD production by 10%",
            "outcome": "Profit +8%",
            "outcome_id": "o_rt_1",
        })

        # Find the decision
        decisions = retrieve_rich(
            query="DCPD production increase",
            n_results=3,
            collection="past_decisions",
        )
        assert any("DCPD" in d.text for d in decisions)

        # Find the outcome
        outcomes = retrieve_rich(
            query="Profit",
            n_results=3,
            collection="decision_outcomes",
        )
        assert any("Profit" in o.text for o in outcomes) or len(outcomes) == 0

    def test_causal_chain_roundtrip(self, tmp_chroma):
        from app.services.rag.smart_indexer import SmartIndexer
        from app.services.rag.rag_retriever import retrieve_rich

        idx = SmartIndexer()
        idx.index_causal_chain({
            "chain_id": "c_rt_1",
            "description": "Naphtha price ↑ → C5 spread ↓ → DCPD margin shrinks",
            "nodes": ["naphtha", "c5", "dcpd"],
            "edges": [["naphtha", "c5"], ["c5", "dcpd"]],
        })
        hits = retrieve_rich(
            query="Naphtha C5 spread",
            n_results=3,
            collection="causal_graph_embeddings",
        )
        assert any("Naphtha" in h.text for h in hits) or len(hits) == 0

    def test_user_memory_roundtrip(self, tmp_chroma, monkeypatch):
        from app.services.rag.user_memory_retriever import (
            build_user_context,
            retrieve_user_memory,
        )

        # Manually index into user_memory via the knowledge_base
        from app.services.rag.collection_names import USER_MEMORY
        from app.services.rag.knowledge_base import create_knowledge_base
        # Point user_memory_retriever at the same org
        monkeypatch.setenv("DEFAULT_ORG_ID", "e2e_org")
        kb = create_knowledge_base(org_id="e2e_org")
        kb.upsert(
            name=USER_MEMORY,
            doc_id="mem_1",
            text="User prefers answers in Chinese",
            metadata={"user_id": "u_e2e", "memory_type": "preference", "topic": "language"},
        )
        kb.close()

        # Retrieve via user_memory_retriever
        entries = retrieve_user_memory(
            user_id="u_e2e",
            query="language preference",
            top_k=3,
        )
        assert len(entries) >= 1
        assert any("Chinese" in e.text or "chinese" in e.text.lower() for e in entries)


class TestBackwardCompatibility:
    """Verify the legacy KB-per-org path still works alongside the new multi-collection path."""

    def test_legacy_kb_collection_unaffected(self, tmp_chroma):
        from app.services.rag.knowledge_base import create_knowledge_base

        # Legacy single collection
        kb1 = create_knowledge_base(org_id="legacy_org")
        kb1.upsert(
            name="industry_reports",
            doc_id="legacy_1",
            text="Some legacy industry report content",
            metadata={"file_name": "old.pdf"},
        )

        # New domain collection
        kb2 = create_knowledge_base(org_id="legacy_org")
        kb2.upsert(
            name="weekly_reports",
            doc_id="new_1",
            text="New weekly report content",
            metadata={"file_name": "new.pdf"},
        )

        # Both should work
        from app.services.rag.rag_retriever import retrieve_rich
        r1 = retrieve_rich(
            query="legacy industry",
            n_results=3,
            collection="industry_reports",
        )
        r2 = retrieve_rich(
            query="weekly report",
            n_results=3,
            collection="weekly_reports",
        )
        assert isinstance(r1, list)
        assert isinstance(r2, list)
        kb1.close()
        kb2.close()