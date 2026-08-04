"""Tests for the rag_retriever module.

The retriever provides a stable interface to query a RAG knowledge base
and get back ``RetrievedChunk`` objects suitable for prompt injection.

Public API (port of EDIA 5.1.2's rag_retriever, adapted to zhanlu's
``app.services.rag.knowledge_base``):
    retrieve(query, n_results=3, collection="industry_reports")
    retrieve_rich(query, n_results=3, collection="industry_reports")
    retrieve_with_context(query, n_results=3, max_chars=1800, collection="industry_reports")
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.rag import rag_retriever
from app.services.rag.rag_retriever import (
    KNOWN_COLLECTIONS,
    RetrievedChunk,
    _normalize_scores,
    retrieve,
    retrieve_rich,
    retrieve_with_context,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_kb(monkeypatch):
    """Patch the knowledge base factory with a MagicMock KB."""
    fake = MagicMock()
    fake_collection = MagicMock()
    fake_collection.query.return_value = {
        "documents": [["chunk text 1", "chunk text 2"]],
        "metadatas": [[
            {"source": "file_a.md", "file_stem": "file_a", "chunk_index": 0, "total_chunks": 5},
            {"source": "file_b.md", "file_stem": "file_b", "chunk_index": 1, "total_chunks": 3},
        ]],
        "distances": [[0.1, 0.4]],
    }
    fake.collection = fake_collection
    # Wire get_collection to return the same fake_collection
    fake.get_collection.return_value = fake_collection
    fake.list_collections.return_value = list(KNOWN_COLLECTIONS)
    # No hybrid_query by default — falls back to dense-only `retrieve()`.
    del fake.hybrid_query

    monkeypatch.setattr(rag_retriever, "_get_kb", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# RetrievedChunk dataclass
# ---------------------------------------------------------------------------


class TestRetrievedChunk:
    def test_formatted_default(self):
        c = RetrievedChunk(
            text="hello world",
            source="docs/file.md",
            file_stem="file",
            chunk_index=0,
            total_chunks=10,
            score=0.95,
            source_label="My Doc",
        )
        out = c.formatted()
        assert "[Source: My Doc](1/10)" in out
        assert "hello world" in out

    def test_formatted_truncates(self):
        long = "a" * 1000
        c = RetrievedChunk(
            text=long,
            source="x.md",
            file_stem="x",
            chunk_index=0,
            total_chunks=1,
            score=1.0,
        )
        out = c.formatted(max_chars=50)
        assert "…" in out
        assert len(out) < 200

    def test_formatted_newlines_replaced(self):
        c = RetrievedChunk(
            text="line1\nline2\nline3",
            source="x.md",
            file_stem="x",
            chunk_index=0,
            total_chunks=1,
            score=1.0,
        )
        out = c.formatted()
        assert "\n" not in out
        assert "line1 line2 line3" in out


class TestNormalizeScores:
    def test_empty(self):
        assert _normalize_scores([]) == []

    def test_all_same_distance(self):
        results = [{"distance": 0.5}, {"distance": 0.5}]
        out = _normalize_scores(results)
        assert all(r["score"] == 1.0 for r in out)

    def test_different_distances(self):
        results = [{"distance": 0.1}, {"distance": 0.5}]
        out = _normalize_scores(results)
        # Lower distance → higher score
        assert out[0]["score"] > out[1]["score"]


# ---------------------------------------------------------------------------
# retrieve() — simple text-based
# ---------------------------------------------------------------------------


class TestRetrieve:
    def test_empty_query_returns_empty(self):
        assert retrieve("") == []
        assert retrieve("   ") == []

    def test_returns_chunks(self, mock_kb):
        out = retrieve("what is the price of DCPD?", n_results=2)
        assert len(out) > 0
        assert all(isinstance(c, RetrievedChunk) for c in out)

    def test_calls_collection_query(self, mock_kb):
        retrieve("foo", n_results=3, collection="weekly_reports")
        # Should have called some collection's query method
        assert mock_kb.collection.query.called

    def test_known_collections_constant(self):
        # KNOWN_COLLECTIONS must include the 9 EDIA semantic collections
        assert "industry_reports" in KNOWN_COLLECTIONS
        assert "weekly_reports" in KNOWN_COLLECTIONS
        assert "past_decisions" in KNOWN_COLLECTIONS
        assert "user_memory" in KNOWN_COLLECTIONS

    def test_unknown_collection_does_not_raise(self, mock_kb):
        # Should not raise even for unknown collection
        out = retrieve("foo", n_results=2, collection="does_not_exist")
        assert isinstance(out, list)


# ---------------------------------------------------------------------------
# retrieve_rich() — with scores
# ---------------------------------------------------------------------------


class TestRetrieveRich:
    def test_returns_scored_chunks(self, mock_kb):
        out = retrieve_rich("test", n_results=2, collection="industry_reports")
        assert len(out) == 2
        assert out[0].score > 0
        assert out[0].source == "file_a.md"
        assert out[0].file_stem == "file_a"
        assert out[0].chunk_index == 0

    def test_chunk_text_populated(self, mock_kb):
        out = retrieve_rich("test", n_results=2)
        assert out[0].text == "chunk text 1"

    def test_empty_query_returns_empty(self, mock_kb):
        assert retrieve_rich("") == []

    def test_falls_back_on_kb_error(self, monkeypatch):
        # Force the factory to return None
        monkeypatch.setattr(rag_retriever, "_get_kb", lambda: None)
        out = retrieve_rich("foo", n_results=3)
        assert out == []


# ---------------------------------------------------------------------------
# retrieve_with_context() — formatted string
# ---------------------------------------------------------------------------


class TestRetrieveWithContext:
    def test_empty_when_no_results(self, mock_kb):
        mock_kb.collection.query.return_value = {
            "documents": [[]], "metadatas": [[]], "distances": [[]]
        }
        out = retrieve_with_context("nothing matches", n_results=3)
        assert out == ""

    def test_formatted_header(self, mock_kb):
        out = retrieve_with_context("test", n_results=2, max_chars=2000)
        assert "[Knowledge Base - Top" in out
        assert "Source:" in out

    def test_max_chars_caps_output(self, mock_kb):
        # Force lots of long chunks
        mock_kb.collection.query.return_value = {
            "documents": [["x" * 1000] * 5],
            "metadatas": [[{"source": f"f{i}.md", "file_stem": f"f{i}",
                            "chunk_index": 0, "total_chunks": 1} for i in range(5)]],
            "distances": [[0.1] * 5],
        }
        out = retrieve_with_context("test", n_results=5, max_chars=200)
        assert len(out) <= 1500  # some overhead for header

    def test_includes_separator(self, mock_kb):
        out = retrieve_with_context("test", n_results=3, max_chars=2000)
        # When multiple results, output should use --- separator
        assert "---" in out
