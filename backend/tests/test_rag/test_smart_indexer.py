"""Tests for the Phase 1 → RAG smart_indexer bridge service.

The smart_indexer provides:
- index_event(event_data): write an event to news_events collection
- index_decision(decision_data): write to past_decisions + decision_outcomes
- index_signal(signal_data): write to market_signals
- index_causal_chain(chain_data): write to causal_graph_embeddings
- index_product(product_data): write to product_catalog
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.rag import smart_indexer
from app.services.rag.smart_indexer import SmartIndexer


@pytest.fixture
def mock_kb(monkeypatch):
    fake = MagicMock()
    fake_collection = MagicMock()
    fake.get_collection.return_value = fake_collection
    monkeypatch.setattr(smart_indexer, "_get_kb", lambda: fake)
    return fake


class TestSmartIndexerInit:
    def test_constructs(self):
        idx = SmartIndexer(org_id="org1")
        assert idx.org_id == "org1"

    def test_default_org_id(self):
        idx = SmartIndexer()
        assert idx.org_id == "default"


class TestIndexEvent:
    def test_index_event_returns_true(self, mock_kb):
        idx = SmartIndexer()
        ok = idx.index_event({
            "event_id": "e1",
            "headline": "OPEC cuts production",
            "key_information": "2M bpd cut",
        })
        assert ok is True

    def test_index_event_handles_missing_key_info(self, mock_kb):
        idx = SmartIndexer()
        ok = idx.index_event({"event_id": "e2", "headline": "News"})
        assert ok is True

    def test_index_event_handles_empty(self, mock_kb):
        idx = SmartIndexer()
        assert idx.index_event({}) is False

    def test_kb_failure_returns_false(self, monkeypatch):
        # Patch the module-level helper that SmartIndexer._kb calls
        from app.services.rag import knowledge_base as kb_mod
        monkeypatch.setattr(kb_mod, "create_knowledge_base", lambda org_id=None: None)
        idx = SmartIndexer()
        assert idx.index_event({"event_id": "e1", "headline": "x"}) is False


class TestIndexDecision:
    def test_indexes_past_decision(self, mock_kb):
        idx = SmartIndexer()
        ok = idx.index_decision({
            "decision_id": "d1",
            "summary": "Buy DCPD",
        })
        assert ok is True

    def test_indexes_outcome(self, mock_kb):
        idx = SmartIndexer()
        ok = idx.index_decision({
            "decision_id": "d1",
            "summary": "Buy DCPD",
            "outcome": "Profit +5%",
            "outcome_id": "o1",
        })
        assert ok is True

    def test_handles_empty(self, mock_kb):
        idx = SmartIndexer()
        assert idx.index_decision({}) is False


class TestIndexSignal:
    def test_index_signal(self, mock_kb):
        idx = SmartIndexer()
        ok = idx.index_signal({
            "signal_id": "s1",
            "description": "DCPD price up 5%",
        })
        assert ok is True

    def test_index_signal_empty(self, mock_kb):
        idx = SmartIndexer()
        assert idx.index_signal({}) is False


class TestIndexCausalChain:
    def test_index_chain(self, mock_kb):
        idx = SmartIndexer()
        ok = idx.index_causal_chain({
            "chain_id": "c1",
            "description": "Naphtha price ↑ → C5 spread ↓",
            "nodes": ["naphtha", "c5"],
            "edges": [["naphtha", "c5"]],
        })
        assert ok is True

    def test_index_chain_empty(self, mock_kb):
        idx = SmartIndexer()
        assert idx.index_causal_chain({}) is False


class TestIndexProduct:
    def test_index_product(self, mock_kb):
        idx = SmartIndexer()
        ok = idx.index_product({
            "product_id": "p1",
            "name": "DCPD",
            "aliases": ["dicyclopentadiene"],
            "category": "C5",
        })
        assert ok is True

    def test_index_product_minimal(self, mock_kb):
        idx = SmartIndexer()
        ok = idx.index_product({"product_id": "p2", "name": "C9"})
        assert ok is True

    def test_index_product_empty(self, mock_kb):
        idx = SmartIndexer()
        assert idx.index_product({}) is False


class TestBridgeIntegration:
    """Phase 1 intelligence layer integration helpers."""

    def test_intelligence_event_to_indexer(self, mock_kb):
        # Mimic what an ExtractedEvent from the intelligence layer would feed
        # in — smart_indexer is duck-typed and accepts plain dicts.
        ev = {
            "event_id": "ev1",
            "event_type": "supply_disruption",
            "headline": "OPEC production cut announced",
            "affected_commodities": ["naphtha"],
            "direction": "down",
            "magnitude_estimate": "medium",
            "certainty": "high",
            "time_horizon": "short_term",
            "geographic_scope": "global",
            "relevance_to_c5_c9": "high",
            "key_information": "2M bpd for Q3",
        }
        idx = SmartIndexer()
        ok = idx.index_event(ev)
        assert ok is True