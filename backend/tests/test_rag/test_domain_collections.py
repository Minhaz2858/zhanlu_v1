"""Tests for domain_collections services.

5 domain-specific collection services (consolidated):
- decision_service: past_decisions + decision_outcomes
- signal_service: market_signals
- causal_service: causal_graph_embeddings
- news_service: news_events (Phase 1 bridge)
- product_service: product_catalog

Each provides upsert/query helpers bound to its named ChromaDB collection.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.rag import domain_collections
from app.services.rag.domain_collections import services as _services_mod
from app.services.rag.domain_collections import (
    CausalService,
    DecisionService,
    NewsService,
    ProductService,
    SignalService,
    build_domain_context,
    get_service,
    list_all_services,
)
from app.services.rag.collection_names import (
    CAUSAL_GRAPH_EMBEDDINGS,
    DECISION_OUTCOMES,
    MARKET_SIGNALS,
    NEWS_EVENTS,
    PAST_DECISIONS,
    PRODUCT_CATALOG,
)


@pytest.fixture
def mock_kb(monkeypatch):
    fake = MagicMock()
    fake_collection = MagicMock()
    fake_collection.query.return_value = {
        "documents": [["sample text"]],
        "metadatas": [[{"source": "test"}]],
        "distances": [[0.1]],
    }
    fake.get_collection.return_value = fake_collection
    monkeypatch.setattr(_services_mod, "_get_kb", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# DecisionService
# ---------------------------------------------------------------------------


class TestDecisionService:
    def test_collection_names(self):
        s = DecisionService()
        assert s.past_decisions_collection == PAST_DECISIONS
        assert s.outcomes_collection == DECISION_OUTCOMES

    def test_upsert_decision_returns_true(self, mock_kb):
        s = DecisionService()
        ok = s.upsert_decision("d1", "Buy DCPD", metadata={"price": 100})
        assert ok is True

    def test_upsert_outcome_returns_true(self, mock_kb):
        s = DecisionService()
        ok = s.upsert_outcome("o1", "Profit +5%", metadata={"decision_id": "d1"})
        assert ok is True

    def test_query_decisions(self, mock_kb):
        s = DecisionService()
        out = s.query_decisions("DCPD", top_k=3)
        assert isinstance(out, list)


# ---------------------------------------------------------------------------
# SignalService
# ---------------------------------------------------------------------------


class TestSignalService:
    def test_collection_name(self):
        assert SignalService().collection_name == MARKET_SIGNALS

    def test_upsert_returns_true(self, mock_kb):
        assert SignalService().upsert("s1", "DCPD price up 5%") is True

    def test_query(self, mock_kb):
        out = SignalService().query("price signal", top_k=3)
        assert isinstance(out, list)


# ---------------------------------------------------------------------------
# CausalService
# ---------------------------------------------------------------------------


class TestCausalService:
    def test_collection_name(self):
        assert CausalService().collection_name == CAUSAL_GRAPH_EMBEDDINGS

    def test_upsert_chain(self, mock_kb):
        ok = CausalService().upsert_chain(
            chain_id="c1",
            description="Naphtha price ↑ → C5 spread ↓",
            nodes=["naphtha", "c5"],
            edges=[("naphtha", "c5")],
        )
        assert ok is True

    def test_query(self, mock_kb):
        out = CausalService().query("naphtha impact", top_k=3)
        assert isinstance(out, list)


# ---------------------------------------------------------------------------
# NewsService (Phase 1 bridge)
# ---------------------------------------------------------------------------


class TestNewsService:
    def test_collection_name(self):
        assert NewsService().collection_name == NEWS_EVENTS

    def test_upsert_event(self, mock_kb):
        ok = NewsService().upsert_event(
            event_id="e1",
            headline="OPEC announces production cut",
            key_information="2M bpd cut for Q3",
            metadata={"source": "Reuters", "category": "supply"},
        )
        assert ok is True

    def test_upsert_event_minimal(self, mock_kb):
        ok = NewsService().upsert_event(event_id="e2", headline="Brief news")
        assert ok is True

    def test_query(self, mock_kb):
        out = NewsService().query("OPEC news", top_k=3)
        assert isinstance(out, list)


# ---------------------------------------------------------------------------
# ProductService
# ---------------------------------------------------------------------------


class TestProductService:
    def test_collection_name(self):
        assert ProductService().collection_name == PRODUCT_CATALOG

    def test_upsert_product(self, mock_kb):
        ok = ProductService().upsert_product(
            product_id="p1",
            name="DCPD",
            aliases=["dicyclopentadiene", "双环戊二烯"],
            category="C5",
        )
        assert ok is True

    def test_query_by_alias(self, mock_kb):
        out = ProductService().query("双环戊二烯", top_k=3)
        assert isinstance(out, list)


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_get_service_decision(self):
        assert isinstance(get_service("decision"), DecisionService)

    def test_get_service_signal(self):
        assert isinstance(get_service("signal"), SignalService)

    def test_get_service_causal(self):
        assert isinstance(get_service("causal"), CausalService)

    def test_get_service_news(self):
        assert isinstance(get_service("news"), NewsService)

    def test_get_service_product(self):
        assert isinstance(get_service("product"), ProductService)

    def test_get_service_unknown_returns_none(self):
        assert get_service("nonexistent") is None

    def test_list_all_services(self):
        services = list_all_services()
        assert len(services) == 5
        assert "decision" in services
        assert "signal" in services
        assert "causal" in services
        assert "news" in services
        assert "product" in services


# ---------------------------------------------------------------------------
# build_domain_context
# ---------------------------------------------------------------------------


class TestBuildDomainContext:
    def test_returns_string(self, mock_kb):
        out = build_domain_context("DCPD price", collections=["news_events"])
        assert isinstance(out, str)

    def test_empty_collections_returns_empty(self, mock_kb):
        assert build_domain_context("anything", collections=[]) == ""

    def test_unknown_collection_skipped(self, mock_kb):
        # Should not raise
        out = build_domain_context("anything", collections=["nonexistent"])
        assert isinstance(out, str)